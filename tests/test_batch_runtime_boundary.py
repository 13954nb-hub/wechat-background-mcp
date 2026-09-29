"""Offline regression for the guarded multi-conversation read boundary."""
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch, Mock

from mcp.server.fastmcp.exceptions import ToolError
from wxbg import gateway, supervisor, worker
from test_readstore_public import _background, _cleanup, _readstore_evidence
import test_readstore_worker as worker_tests
import test_readstore_supervisor as supervisor_tests

EPOCH = 'b' * 64
ROOMS = ['alice', 'project@chatroom']


def cursor(room, high=11, *, gap=False):
    position = {'shard_id': 'message_0', 'rowid': high,
                'local_id': high, 'sort_seq': high}
    return {'version': 3, 'chat_md5': hashlib.md5(room.encode()).hexdigest(),
            'direction': 'asc', 'filter': None, 'shard_ids': ['message_0'],
            'account_epoch': EPOCH, 'shard_highwater': [dict(position)],
            'shard_boundary': [dict(position)], 'gap_detected': gap}


def request():
    return {'account_epoch': EPOCH, 'max_total': 4,
            'conversations': [{'conversation_key': key, 'cursor': None,
                               'start_from': 'beginning', 'limit': 2} for key in ROOMS]}


def response():
    states = []
    for key in ROOMS:
        identity = {'chat_md5': hashlib.md5(key.encode()).hexdigest(),
                    'shard_id': 'message_0', 'local_id': 11, 'server_id': 51, 'rowid': 11}
        row = {'conversation_key': key, 'account_epoch': EPOCH,
               'shard_id': 'message_0', 'local_id': 11, 'sort_seq': 11,
               'local_type': 1, 'sender_id': 1, 'server_id': 51, 'create_time': 100,
               'content': 'hello', 'content_available': True, 'content_truncated': False,
               'message_identity': identity}
        states.append({'conversation_key': key, 'items': [row], 'next_cursor': cursor(key),
                       'has_more': False, 'gap_detected': False})
    evidence = _readstore_evidence()
    for field in ('captures', 'snapshots'):
        evidence[field]['message_0'] = copy.deepcopy(evidence[field]['session'])
    batch = {'ok': True, 'status': 'complete',
             'verification_level': 'authenticated_readstore_batch',
             'account_epoch': EPOCH, 'conversations': states,
             'counts': {'conversations': 2, 'items': 2},
             'coverage': 'bounded_authenticated_batch',
             'ordering': 'rowid_continuation_not_chronological',
             'bounded': True, 'full_history': False, 'exact_once': False,
             'readstore_account_epoch': EPOCH, 'readstore_evidence': evidence}
    return {'ok': True, 'worker_started': True, 'target_validation': {'status': 'stable'},
            'result': batch, 'evidence': _background(), 'cleanup': _cleanup()}


class BatchPublicTests(unittest.TestCase):
    def project(self, value, args=None):
        from wxbg.readstore_batch_public import batch_result_or_error
        return batch_result_or_error(value, request=request() if args is None else args)

    def reject(self, value, args=None):
        with self.assertRaises(ToolError) as failure:
            self.project(value, args)
        self.assertNotIn('SECRET', str(failure.exception))

    def test_projects_two_rooms_preserves_cursors_and_strips_private_evidence(self):
        value = self.project(response())
        self.assertEqual(value['result']['counts'], {'conversations': 2, 'items': 2})
        self.assertNotIn('SECRET', json.dumps(value))
        self.assertEqual([s['next_cursor']['version'] for s in value['result']['conversations']], [3, 3])
        self.assertFalse(value['result']['multi_database_atomic'])

    def test_projection_accepts_formal_rows_without_decimal_identity_extension(self):
        try:
            value = self.project(response())
        except Exception as error:
            self.fail(f'formal message_identity shape must remain sufficient: {type(error).__name__}: {error}')
        row = value['result']['conversations'][0]['items'][0]
        self.assertEqual(row['message_identity']['local_id'], 11)
        self.assertNotIn('message_identity_decimal', row)

    def test_rejects_failed_guard_cleanup_or_account_drift(self):
        for section, key, value in [('evidence', 'background_observation_passed', False),
                                    ('cleanup', 'modified', True),
                                    ('result', 'readstore_account_epoch', 'other')]:
            with self.subTest(section=section, key=key):
                raw = response(); raw[section][key] = value
                self.reject(raw)

    def test_rejects_foreign_room_and_private_row_field(self):
        for key, value in [('conversation_key', 'mallory'), ('private_path', 'SECRET')]:
            raw = response(); raw['result']['conversations'][0]['items'][0][key] = value
            self.reject(raw)

    def test_rejects_cursor_behind_returned_row_and_unknown_filter(self):
        for changed in [cursor(ROOMS[0], 10), {**cursor(ROOMS[0]), 'filter': 'arbitrary'}]:
            raw = response(); raw['result']['conversations'][0]['next_cursor'] = changed
            self.reject(raw)

    def test_rejects_cursor_shards_not_in_authenticated_snapshot(self):
        raw = response(); c = raw['result']['conversations'][0]['next_cursor']
        c['shard_ids'] = ['message_1']
        for field in ('shard_highwater', 'shard_boundary'):
            c[field][0]['shard_id'] = 'message_1'
        self.reject(raw)

    def test_rejects_replayed_rows_and_regressing_cursor_without_gap(self):
        args = request()
        args['conversations'][0].update(cursor=cursor(ROOMS[0], 11), start_from='cursor')
        self.reject(response(), args)
        raw = response(); raw['result']['conversations'][0]['items'] = []
        raw['result']['conversations'][0]['next_cursor'] = cursor(ROOMS[0], 10)
        raw['result']['counts']['items'] = 1
        self.reject(raw, args)

    def test_rejects_fresh_beginning_empty_page_that_skips_existing_rows(self):
        raw = response()
        for state in raw['result']['conversations']:
            state['items'] = []
        raw['result']['counts']['items'] = 0
        self.reject(raw)

    def test_preserves_caught_up_cursor_on_continuation(self):
        args = request()
        raw = response()
        for spec, state in zip(args['conversations'], raw['result']['conversations']):
            spec.update(cursor=cursor(spec['conversation_key']), start_from='cursor')
            state['items'] = []
        raw['result']['counts']['items'] = 0
        self.assertEqual(self.project(raw, args)['result']['counts']['items'], 0)

    def test_fresh_now_can_anchor_empty_page_at_witness(self):
        args = request()
        raw = response()
        for spec, state in zip(args['conversations'], raw['result']['conversations']):
            spec['start_from'] = 'now'
            state['items'] = []
            state['next_cursor']['filter'] = 'start_from:now'
        raw['result']['counts']['items'] = 0
        self.assertEqual(self.project(raw, args)['result']['counts']['items'], 0)

    def test_rejects_cursor_ahead_of_returned_row(self):
        raw = response()
        raw['result']['conversations'][0]['next_cursor'] = cursor(ROOMS[0], 12)
        self.reject(raw)

    def test_rejects_cursor_boundary_that_differs_from_highwater(self):
        raw = response()
        raw['result']['conversations'][0]['next_cursor']['shard_boundary'][0]['rowid'] = 12
        self.reject(raw)

    def test_gap_cannot_regress_cursor_or_change_filter(self):
        args = request()
        args['conversations'][0].update(cursor=cursor(ROOMS[0], 11), start_from='cursor')
        raw = response(); state = raw['result']['conversations'][0]
        state.update(items=[], gap_detected=True, next_cursor=cursor(ROOMS[0], 11, gap=True))
        raw['result'].update(status='partial', counts={'conversations': 2, 'items': 1})
        self.assertTrue(self.project(raw, args)['ok'])
        for changed in [cursor(ROOMS[0], 10, gap=True),
                        {**cursor(ROOMS[0], 11, gap=True), 'filter': 'start_from:now'}]:
            value = copy.deepcopy(raw)
            value['result']['conversations'][0]['next_cursor'] = changed
            self.reject(value, args)

    def test_gap_flag_cannot_disappear_on_continuation(self):
        args = request()
        args['conversations'][0].update(cursor=cursor(ROOMS[0], 10, gap=True), start_from='cursor')
        self.reject(response(), args)

    def test_fresh_start_mode_is_bound_to_result(self):
        args = request(); args['conversations'][0]['start_from'] = 'now'
        raw = response()
        raw['result']['conversations'][0]['next_cursor']['filter'] = 'start_from:now'
        self.reject(raw, args)  # A fresh now request cannot return old rows.
        raw = response()
        raw['result']['conversations'][0]['next_cursor']['filter'] = 'start_from:now'
        self.reject(raw)  # Beginning cannot silently anchor at now.

    def test_output_cap_applies_to_whole_batch(self):
        with patch('wxbg.readstore_batch_public.MAX_BATCH_PUBLIC_BYTES', 100):
            self.reject(response())


class BatchGatewayTests(unittest.TestCase):
    def test_guardian_imports_candidate_with_competing_package_in_original_cwd(self):
        source_root = str(Path(gateway.__file__).resolve().parents[1])
        with tempfile.TemporaryDirectory() as directory:
            rogue = Path(directory, 'wxbg')
            rogue.mkdir()
            (rogue / '__init__.py').write_text('raise RuntimeError("wrong wxbg imported")\n',
                                              encoding='utf-8')
            env = dict(os.environ, PYTHONPATH=os.pathsep.join((source_root, directory)))
            result = subprocess.run([sys.executable, '-B', '-m', 'wxbg.supervisor'],
                                    cwd=source_root, env=env, input='{}',
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)['error']['code'], 'INVALID_REQUEST')

    def test_relative_state_dir_is_absolute_for_gateway_and_guardian(self):
        source_root = str(Path(gateway.__file__).resolve().parents[1])
        script = ('import sys; sys.path.insert(0, sys.argv[1]); '
                  'from wxbg import gateway; print(gateway.STATE)')
        with tempfile.TemporaryDirectory() as directory:
            env = dict(os.environ, WXBG_STATE_DIR='relative-state')
            result = subprocess.run([sys.executable, '-I', '-B', '-c', script, source_root],
                                    cwd=directory, env=env, capture_output=True,
                                    text=True, check=True)
            self.assertEqual(result.stdout.strip(), str(Path(directory, 'relative-state').resolve()))

    def test_unsupported_cursor_filter_rejected_before_dispatch(self):
        invalid_cursor = {**cursor('alice'), 'filter': 'since_seq:7'}
        with patch.object(gateway, 'execute') as execute:
            with self.assertRaises(ToolError) as failure:
                gateway.wechat_batch_read_messages(EPOCH, [
                    {'conversation_key': 'alice', 'cursor': invalid_cursor, 'limit': 1}])
            self.assertIn('invalid_batch_read_request', str(failure.exception))
            execute.assert_not_called()

    def test_child_uses_same_source_tree_as_gateway(self):
        child = Mock()
        child.communicate.return_value = ('{"ok": true}', '')
        with patch.dict(os.environ, {'PYTHONPATH': 'unrelated-source'}), \
                patch.object(gateway.subprocess, 'Popen', return_value=child) as launch:
            self.assertTrue(gateway.execute('readstore_batch_read_new', request())['ok'])
        pythonpath = launch.call_args.kwargs['env']['PYTHONPATH'].split(os.pathsep)
        source_root = str(Path(gateway.__file__).resolve().parents[1])
        self.assertEqual(pythonpath[0], source_root)
        # `python -m` searches CWD ahead of PYTHONPATH; bind that root too.
        self.assertEqual(launch.call_args.kwargs['cwd'], source_root)

    def test_sdk_rejects_unknown_nested_parameters(self):
        tool = gateway.SERVER._tool_manager.get_tool('wechat_batch_read_messages')
        with self.assertRaises(ValueError):
            tool.fn_metadata.arg_model.model_validate({'account_epoch': EPOCH,
                'conversations': [{'conversation_key': 'alice', 'private_path': 'SECRET'}]})

    def test_request_defaults_and_read_only_schema(self):
        with patch.object(gateway, 'execute', return_value=response()) as execute:
            result = gateway.wechat_batch_read_messages(EPOCH, [
                {'conversation_key': key, 'limit': 2, 'start_from': 'beginning'} for key in ROOMS], max_total=4)
        self.assertTrue(result['ok'])
        execute.assert_called_once_with('readstore_batch_read_new', request())
        tool = gateway.SERVER._tool_manager.get_tool('wechat_batch_read_messages')
        self.assertTrue(tool.annotations.readOnlyHint)
        self.assertFalse(tool.parameters['additionalProperties'])

    def test_invalid_requests_never_dispatch(self):
        for args in [[], [{'conversation_key': 'alice', 'limit': True}],
                     [{'conversation_key': 'alice', 'private_path': 'SECRET'}],
                     [{'conversation_key': 'alice'}] * 2]:
            with self.subTest(args=args), patch.object(gateway, 'execute') as execute:
                with self.assertRaises(ToolError):
                    gateway.wechat_batch_read_messages(EPOCH, args)
                execute.assert_not_called()

    def test_budget_and_mismatched_cursor_rejected_before_dispatch(self):
        for args in [[{'conversation_key': 'alice', 'limit': 5}],
                     [{'conversation_key': 'alice', 'cursor': cursor('mallory')}]]:
            with patch.object(gateway, 'execute') as execute:
                with self.assertRaises(ToolError):
                    gateway.wechat_batch_read_messages(EPOCH, args, max_total=4)
                execute.assert_not_called()


class BatchRoutingTests(unittest.TestCase):
    def test_worker_routes_batch_without_adapter(self):
        helper = worker_tests.ReadstoreWorkerTests(); helper.setUp()
        seen = []
        result = helper.invoke('readstore_batch_read_new',
            provider=lambda action, args, **kw: seen.append(action) or {'bounded': True},
            adapter=lambda *_: self.fail('UI adapter constructed'))
        self.assertTrue(result['ok'], result)
        self.assertEqual(seen, ['readstore_batch_read_new'])

    def test_supervisor_batch_has_read_only_cleanup_and_no_gate_write(self):
        helper = supervisor_tests.ReadstoreSupervisorTests(); helper.setUp()
        try:
            result = helper.run_request('readstore_batch_read_new',
                lambda *_: {'ok': True, 'result': {'bounded': True}})
            self.assertTrue(result['ok'], result)
            self.assertEqual(helper.backend.writes, [])
            self.assertEqual(result['cleanup']['status'], 'read_only')
        finally:
            helper.doCleanups()


if __name__ == '__main__':
    unittest.main()
