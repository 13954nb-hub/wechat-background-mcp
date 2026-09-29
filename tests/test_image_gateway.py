"""Public image MCP validation and durable deduplication without client operations."""
import asyncio
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
import zlib
from mcp.server.fastmcp.exceptions import ToolError
from wxbg.journal import Journal
from test_gateway import load_isolated_gateway
from test_file_card_integration import _background_evidence
from test_image_entry_probe import native_report

REF='a'*32


def png():
    def chunk(kind,data):
        return struct.pack('>I',len(data))+kind+data+struct.pack('>I',zlib.crc32(kind+data))
    return b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',1,1,8,2,0,0,0))+chunk(b'IDAT',zlib.compress(b'\0\x10\x20\x30'))+chunk(b'IEND',b'')


def success():
    frame=dict(draft_kind='empty',row_count=8,runtime_sha256='a'*64,semantic_sha256='b'*64,
        rebound_count=0,ignored_rebound_total=1,new_rows_count=1,rows_seen=True,
        image_novelty=True,embedded_observations=0)
    result=dict(ok=True,status='local_image_transition_observed',
        verification_level='stable_local_image_ui_transition',
        counts=dict(native_selections=1,send_clicks=1,new_rows=1),refs=dict(conversation=REF),
        background_mode='minimized',remote_receipt_verified=False,upload_status='unknown')
    return dict(ok=True,worker_started=True,result=result,cleanup=dict(restored=True,observed=0,errors=[]),
        evidence=_background_evidence(),native_evidence=native_report(),image_evidence_valid=True,
        image_evidence=dict(version=1,baseline=dict(row_count=7,runtime_sha256='c'*64,semantic_sha256='d'*64),
            selection_frames=[],final_frames=[dict(frame,index=1,stable_count=1),dict(frame,index=2,stable_count=2)]))


class ImageGatewayTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root=Path(temp.name);self.gateway=load_isolated_gateway(self.root/'state')
        self.path=self.root/'owned.png';self.path.write_bytes(png())
        self.execute=self.enterContext(patch.object(self.gateway,'execute',return_value=success()))

    def send(self,operation='image-one',**kwargs):
        return self.gateway.wechat_send_image(session_ref=REF,path=str(self.path),operation_id=operation,**kwargs)

    def test_public_summary_and_replay_are_compact_and_dispatch_once(self):
        first=self.send();second=self.send()
        self.assertEqual(self.execute.call_count,1)
        self.assertEqual(first['result'],success()['result'])
        self.assertEqual(set(first),{'ok','operation_id','replayed','result',
                                     'sender_role','sender_role_verified','sender_role_evidence'})
        self.assertFalse(first['replayed']);self.assertTrue(second['replayed'])
        self.assertEqual(first['result'],second['result'])
        self.assertNotIn('image_evidence',json.dumps(first))
        row=Journal(self.gateway.STATE/'operations.sqlite3').get('image-one')
        self.assertEqual(row['state'],'complete')

    def test_invalid_png_and_suffix_stop_before_journal_or_dispatch(self):
        self.path.write_bytes(b'not an image')
        with self.assertRaises(ToolError):self.send()
        self.execute.assert_not_called()
        self.assertIsNone(Journal(self.gateway.STATE/'operations.sqlite3').get('image-one'))
        with self.assertRaises(ToolError):
            self.gateway.wechat_send_image(REF,str(self.root/'fake.jpg'),'wrong-type')

    def test_success_without_any_one_evidence_channel_is_sticky_unknown(self):
        for key in ('image_evidence','image_evidence_valid','native_evidence','cleanup','evidence'):
            response=success();response.pop(key);self.execute.return_value=response
            operation='missing-'+key
            with self.subTest(key=key),self.assertRaises(ToolError):self.send(operation)
            row=Journal(self.gateway.STATE/'operations.sqlite3').get(operation)
            self.assertEqual(row['state'],'outcome_unknown')
            count=self.execute.call_count
            with self.assertRaises(ToolError):self.send(operation)
            self.assertEqual(self.execute.call_count,count)

    def test_changed_bytes_conflict_and_hash_mismatch_never_dispatch_again(self):
        self.send();count=self.execute.call_count
        with self.assertRaises(ToolError):self.send(expected_sha256='f'*64)
        self.assertEqual(self.execute.call_count,count)
        with self.assertRaises(ToolError):self.gateway.wechat_send_image('bad-ref',str(self.path),'new')
        self.assertEqual(self.execute.call_count,count)

    def test_actual_mcp_schema_rejects_non_string_and_extra_fields(self):
        for args in (dict(session_ref=REF,path=42,operation_id='bad'),
                     dict(session_ref=REF,path=str(self.path),operation_id='bad',extra=True)):
            with self.subTest(args=args),self.assertRaises(ToolError):
                asyncio.run(self.gateway.SERVER.call_tool('wechat_send_image',args))
        self.execute.assert_not_called()


if __name__=='__main__':unittest.main()
