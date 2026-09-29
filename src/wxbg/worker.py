"""One bounded UIA operation; supervisor alone owns memory and recovery."""
import json
import math
import sys
import time
from .policy import AdapterError


READSTORE_ACTIONS = frozenset({
    'readstore_read_inbox',
    'readstore_search',
    'readstore_read_new',
    'readstore_batch_read_new',
    'readstore_get_attachment',
})
MAX_READSTORE_RESULT_BYTES = 512 * 1024
MAX_ATTACHMENT_RESULT_BYTES = 24 * 1024 * 1024
_READSTORE_SAFE_ERRORS = frozenset({
    'readstore_provider_unavailable', 'readstore_provider_failed',
    'readstore_result_invalid', 'readstore_invalid_deadline',
    'readstore_deadline', 'readstore_background_failed',
    'readstore_input_invalid', 'readstore_size_limit',
    'readstore_source_changed', 'readstore_account_changed',
    'readstore_unavailable', 'readstore_index_invalid',
    'query_budget_exceeded', 'unsupported', 'invalid_limit',
    'invalid_keyword', 'invalid_cursor', 'cursor_context_conflict',
    'invalid_chat_md5', 'missing_epoch', 'schema_unsupported',
    'attachment_not_downloaded', 'attachment_ambiguous', 'attachment_size_limit',
    'attachment_deadline', 'attachment_path_changed', 'attachment_file_changed',
    'attachment_file_unavailable', 'attachment_identity_unavailable',
    'attachment_declaration_invalid', 'attachment_close_failed',
    'attachment_account_changed', 'attachment_message_unavailable',
    'attachment_image_key_unavailable', 'attachment_image_invalid',
})


class _ReadstoreFailure(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _readstore_error_code(error):
    code = getattr(error, 'code', None)
    return code if code in _READSTORE_SAFE_ERRORS else 'readstore_provider_failed'


def _readstore_check_deadline(deadline):
    if type(deadline) not in (int, float) or not math.isfinite(deadline):
        raise _ReadstoreFailure('readstore_invalid_deadline')
    if time.monotonic() >= deadline:
        raise _ReadstoreFailure('readstore_deadline')


def _readstore_background(target):
    try:
        _check_hint_background(target)
    except Exception as exc:
        raise _ReadstoreFailure('readstore_background_failed') from exc


def _bounded_readstore_result(value, *, max_bytes=MAX_READSTORE_RESULT_BYTES):
    if type(value) is not dict:
        raise _ReadstoreFailure('readstore_result_invalid')
    if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_ATTACHMENT_RESULT_BYTES:
        raise _ReadstoreFailure('readstore_result_invalid')
    try:
        encoded = json.dumps(value, ensure_ascii=True, allow_nan=False,
                             separators=(',', ':')).encode('utf-8')
    except Exception as exc:
        raise _ReadstoreFailure('readstore_result_invalid') from exc
    if len(encoded) > max_bytes:
        raise _ReadstoreFailure('readstore_result_invalid')
    return value

def _check_hint_background(target):
    """Native-only target/background check; never constructs a chat adapter."""
    import psutil
    import win32process
    from .monitor import snapshot
    def identity():
        if (win32process.GetWindowThreadProcessId(target['hwnd'])[1] != target['pid']
                or psutil.Process(target['pid']).create_time() != target['created']):
            raise AdapterError('stale_process')
    identity()
    state = snapshot(target['pid'], target['hwnd'])
    identity()
    if not state['minimized']:
        raise AdapterError('background_requires_minimized')
    if state['foreground'] == target['hwnd']:
        raise AdapterError('wechat_has_foreground')
    if state['visible_windows'] != [target['hwnd']]:
        raise AdapterError('existing_popup')
    if state['capture']:
        raise AdapterError('capture_active')


def _normalize_contact_span_evidence(value):
    try:
        from .contact_span_contract import normalize_contact_span_evidence
        return normalize_contact_span_evidence(value)[0]
    except Exception:
        return {'primary_error_code': 'contact_span_evidence_invalid'}


_FILE_CARD_SAFE_ERRORS = frozenset({
    'invalid_session_ref', 'invalid_message_ref', 'invalid_deadline',
    'file_card_budget_exhausted', 'context_conflict', 'draft_conflict',
    'file_card_not_found', 'ambiguous_file_card', 'file_card_changed',
    'file_card_ref_stale', 'file_card_layout_unverified', 'file_card_read_failed',
})


def _safe_file_card_error(code):
    try:
        from .file_card_contract import safe_file_card_error
        value = safe_file_card_error(code)
    except Exception:
        value = code
    return value if value in _FILE_CARD_SAFE_ERRORS else 'file_card_read_failed'


def _file_card_background_valid(evidence):
    """Match the gateway's side-effect policy; cursor is telemetry only."""
    if not isinstance(evidence, dict):
        return False
    if type(evidence.get('background_observation_passed')) is not bool:
        return False
    if type(evidence.get('observations')) is not int or evidence['observations'] < 1:
        return False
    if evidence.get('foreground_changed') is not False:
        return False
    if evidence.get('clipboard_changed') is not False:
        return False
    if evidence.get('target_restored') is not False:
        return False
    if evidence.get('capture_observed') is not False:
        return False
    if evidence.get('new_visible_windows') != []:
        return False
    if evidence.get('monitor_errors') != []:
        return False
    return evidence['background_observation_passed'] is True


def _scan_background_valid(evidence, monitor, target):
    """Keep Chats minimized while allowing unrelated apps to trade focus.

    This applies only to the bounded session scan. It still rejects mouse
    movement, clipboard changes, target focus/restore, popups and uncertainty.
    The original global monitor verdict remains in the evidence unchanged.
    """
    try:
        samples = [monitor.before, *monitor.samples, evidence['after']]
        target_foreground = any(sample['foreground'] == target['hwnd']
                                for sample in samples)
        safe = (bool(samples)
                and all(sample['minimized'] is True for sample in samples)
                and not target_foreground
                and type(evidence.get('cursor_changed')) is bool
                and evidence['cursor_changed'] is False
                and evidence.get('clipboard_changed') is False
                and evidence.get('target_restored') is False
                and evidence.get('capture_observed') is False
                and evidence.get('new_visible_windows') == []
                and evidence.get('monitor_errors') == []
                and type(evidence.get('foreground_changed')) is bool
                and type(evidence.get('observations')) is int
                and evidence['observations'] >= 1)
    except Exception:
        target_foreground = True
        safe = False
    evidence['target_foreground_observed'] = target_foreground
    evidence['scan_background_observation_passed'] = safe
    return safe


def _file_card_error(code, *, outcome_unknown=False):
    return {
        'code': code,
        'detail': code,
        'submission_started': False,
        'outcome_unknown': outcome_unknown is True,
    }


_FILE_SEND_PHASE_FIELDS = (
    'native_selection_started',
    'native_selection_completed',
    'embedded_file_staged',
    'send_click_attempted',
    'local_card_observed',
)


def _file_send_phase_evidence(value):
    """Expose fixed milestones only; never copy file/chat/native details."""
    source = value if type(value) is dict else {}
    evidence = {'schema': 'file_send_phase_v1'}
    evidence.update({field: source.get(field) is True for field in _FILE_SEND_PHASE_FIELDS})
    return evidence

def main():
    request=json.loads(sys.stdin.read())
    from .monitor import Monitor
    monitor=None;adapter=None;event_evidence=None;phase_recorder=None
    action=request.get('action')
    hint=action=='wait_for_ui_hint'
    history=action=='scroll_messages'
    span=action=='read_history_span'
    contact_span=action=='read_contact_span'
    image_action=action in ('probe_image_entry','send_image')
    session_scan=action in ('scan_open_session', 'scan_session_viewports')
    file_card=action=='read_file_card'
    readstore=action in READSTORE_ACTIONS
    response={'ok':False,'error':{'code':'worker_failed','detail':'worker_failed',
                                  'submission_started':False}}
    try:
        target=request['target']
        if contact_span or image_action:
            from .phase_recorder import PhaseRecorder
            phase_recorder=PhaseRecorder()
            monitor=Monitor(target['pid'],target['hwnd'],phase_recorder=phase_recorder).start()
        else:
            monitor=Monitor(target['pid'],target['hwnd']).start()
        if hint:
            from .ui_hint import wait_for_ui_hint
            value=wait_for_ui_hint(target,request.get('args',{}).get('timeout_seconds'),
                request.get('deadline'),check_background=lambda:_check_hint_background(target))
            result=value['result'];event_evidence=value['event_evidence']
        elif readstore:
            deadline=request.get('deadline')
            _readstore_check_deadline(deadline)
            _readstore_background(target)
            try:
                from .readstore_provider import run as readstore_run
            except ImportError as exc:
                raise _ReadstoreFailure('readstore_provider_unavailable') from exc
            provider_error=None
            provider_result=None
            try:
                provider_result=readstore_run(
                    action, request.get('args', {}), target=target, deadline=deadline)
            except Exception as exc:
                provider_error=exc
            # Always re-check the background/identity boundary before accepting
            # either a provider result or a provider failure.
            _readstore_background(target)
            if provider_error is not None:
                raise provider_error
            _readstore_check_deadline(deadline)
            result=_bounded_readstore_result(
                provider_result,
                max_bytes=(MAX_ATTACHMENT_RESULT_BYTES
                           if action == 'readstore_get_attachment'
                           else MAX_READSTORE_RESULT_BYTES),
            )
        else:
            from .adapter import Adapter
            # UIA rectangles and Win32 client points must share one physical
            # coordinate space on mixed-DPI desktops. This worker is short lived;
            # only its own UI thread changes awareness, never the Weixin process.
            from .monitor import PM_V2, user32
            previous_dpi=user32.SetThreadDpiAwarenessContext(PM_V2)
            if (not previous_dpi or not user32.AreDpiAwarenessContextsEqual(
                    user32.GetThreadDpiAwarenessContext(), PM_V2)):
                raise AdapterError('coordinate_context_unverified')
            adapter=Adapter(target)
            if image_action:
                adapter.image_phase=phase_recorder.set_phase
            if span:
                from .history_span import collect_history_span
                result=collect_history_span(adapter,deadline=request.get('deadline'),**request.get('args',{}))
            elif contact_span:
                from .contact_span import collect_contact_span
                result=collect_contact_span(adapter,deadline=request.get('deadline'),
                                            phase=phase_recorder.set_phase,
                                            **request.get('args',{}))
            elif file_card:
                from .file_card import read_file_card
                args = request.get('args', {})
                result=read_file_card(adapter, args.get('session_ref'), args.get('message_ref'),
                                      deadline=request.get('deadline'))
            elif session_scan:
                if action == 'scan_session_viewports':
                    from .session_viewport_scan import scan_session_viewports
                    result=scan_session_viewports(
                        adapter, deadline=request.get('deadline'),
                        **request.get('args', {}))
                else:
                    from .session_navigation import scan_open_session
                    result=scan_open_session(adapter, deadline=request.get('deadline'),
                                             **request.get('args', {}))
            elif request.get('action') == 'probe_image_entry':
                from .image_entry_probe import run as probe_image_entry
                result=probe_image_entry(adapter, deadline=request.get('deadline'),
                                         **request.get('args', {}))
            elif request.get('action') == 'send_image':
                from .image_actions import run as send_image
                result=send_image(adapter,deadline=request.get('deadline'),**request.get('args', {}))
            elif history:
                from .history_actions import scroll_messages
                result=scroll_messages(adapter,deadline=request.get('deadline'),**request.get('args',{}))
            else:
                result=adapter.dispatch(request['action'],request.get('args',{}))
        response={'ok':True,'result':result}
    except Exception as exc:
        if readstore:
            code=_readstore_error_code(exc)
            response={'ok':False,'error':{'code':code,'detail':code,
                                          'submission_started':False}}
        else:
            code=exc.code if isinstance(exc,AdapterError) else type(exc).__name__
        if hint:
            event_evidence=getattr(exc,'event_evidence',event_evidence)
            # Provider exception messages can contain text; expose only codes.
            code=code if isinstance(exc,AdapterError) else 'ui_hint_failed'
        if history:
            from .history_contract import safe_history_error
            code=safe_history_error(code)
        if span:
            from .history_span_contract import safe_history_span_error
            code=safe_history_span_error(code)
        if contact_span:
            from .contact_span_contract import safe_contact_span_error
            code=safe_contact_span_error(code)
        if session_scan:
            from .session_scan_contract import safe_scan_error
            code=safe_scan_error(code)
        if file_card:
            code = _safe_file_card_error(code)
            response={'ok':False,'error':_file_card_error(
                code, outcome_unknown=getattr(exc, 'outcome_unknown', False) is True)}
        elif not readstore:
            response={'ok':False,'error':{'code':code,'detail':code if hint or history else str(exc),'submission_started':bool(adapter and adapter.submission_started)}}
        if span:
            response['error']['detail'] = code
        if contact_span:
            response['error']['detail'] = code
        if session_scan:
            response['error']['detail'] = code
        if hint or history or span:
            response['error']['outcome_unknown']=getattr(exc,'outcome_unknown',False) is True
    finally:
        if request.get('action') in ('probe_image_entry','send_image') and adapter is not None:
            from .image_observation_contract import normalize_image_evidence
            response['image_evidence'], response['image_evidence_valid'] = normalize_image_evidence(
                getattr(adapter, 'image_evidence', None))
        if monitor:
            try:
                evidence=monitor.stop();response['evidence']=evidence
                background_valid = (_file_card_background_valid(evidence)
                                    if file_card else
                                    _scan_background_valid(evidence, monitor, target)
                                    if session_scan else evidence['background_observation_passed'])
                if not background_valid:
                    if file_card or readstore:
                        response.pop('result', None)
                        response.pop('worker_result', None)
                    response['ok']=False
                    if file_card:
                        response['error'] = _file_card_error('background_side_effect')
                    elif readstore:
                        response['error']={'code':'background_side_effect',
                                           'detail':'background_side_effect',
                                           'submission_started':False}
                    else:
                        response['error']={'code':'background_side_effect','submission_started':bool(adapter and adapter.submission_started),'detail':'inspect evidence; do not automatically repeat a mutation'}
            except Exception as exc:
                if contact_span:
                    from .contact_span_contract import safe_contact_span_error
                    code = safe_contact_span_error('monitor_failed')
                    response={'ok':False,'error':{'code':code,'detail':code,
                        'submission_started':bool(adapter and adapter.submission_started)}}
                    evidence={'background_observation_passed':False,
                              'monitor_errors':['monitor_failed']}
                    if phase_recorder is not None:
                        try:evidence['phase_summary']=phase_recorder.snapshot()
                        except Exception:pass
                    response['evidence']=evidence
                elif file_card:
                    response={'ok':False,'error':_file_card_error('monitor_failed')}
                    response['evidence']={'background_observation_passed':False,
                                          'monitor_errors':['monitor_failed']}
                elif readstore:
                    response={'ok':False,'error':{'code':'monitor_failed','detail':'monitor_failed',
                                                  'submission_started':False},
                              'evidence':{'background_observation_passed':False,
                                          'monitor_errors':['monitor_failed']}}
                else:
                    response={'ok':False,'error':{'code':'monitor_failed','detail':type(exc).__name__,
                        'submission_started':bool(adapter and adapter.submission_started)}}
        if action == 'send_file':
            evidence = response.get('evidence')
            if type(evidence) is not dict:
                evidence = {}
                response['evidence'] = evidence
            evidence['file_send_phase'] = _file_send_phase_evidence(
                getattr(adapter, 'file_send_phase', None))
        if not file_card and adapter and getattr(adapter,'native_evidence',None) is not None:
            response['native_evidence']=adapter.native_evidence
        if not file_card and adapter and getattr(adapter,'navigation_evidence',None) is not None:
            response['navigation_evidence']=adapter.navigation_evidence
        if hint:
            from .event_contract import normalize_event_evidence, event_cleanup_verified
            response['event_evidence']=normalize_event_evidence(event_evidence)[0]
            if not response['ok']:
                response['error']['outcome_unknown']=(response['error'].get('outcome_unknown') is True
                    or not event_cleanup_verified(response['event_evidence']))
        if history:
            from .history_contract import normalize_history_evidence
            response['history_evidence']=normalize_history_evidence(getattr(adapter,'history_evidence',None))[0]
            if not response['ok']:
                delivered=response['history_evidence']['delivery_started']
                response['error']['submission_started']=False
                response['error']['navigation_started']=delivered
                response['error']['outcome_unknown']=(response['error'].get('outcome_unknown') is True
                    or delivered is not False)
        if span:
            from .history_span_contract import normalize_history_span_evidence
            response['history_span_evidence'] = normalize_history_span_evidence(
                getattr(adapter, 'history_span_evidence', None))[0]
            delivered = response['history_span_evidence']['delivery_started']
            if not response['ok']:
                response.pop('result', None)
                response['error']['submission_started'] = False
                response['error']['navigation_started'] = delivered
                response['error']['outcome_unknown'] = (
                    response['error'].get('outcome_unknown') is True
                    or delivered is not False
                )
        if contact_span:
            response['contact_span_evidence'] = _normalize_contact_span_evidence(
                getattr(adapter, 'contact_span_evidence', None))
            delivered = response['contact_span_evidence'].get('delivery_started')
            if not response['ok']:
                response.pop('result', None)
                response['error']['submission_started'] = False
                response['error']['navigation_started'] = delivered
                response['error']['outcome_unknown'] = (
                    response['error'].get('outcome_unknown') is True
                    or delivered is not False
                )
        if session_scan:
            from .session_scan_contract import normalize_scan_evidence
            scan, shape_valid = normalize_scan_evidence(
                getattr(adapter, 'navigation_evidence', None))
            response['navigation_evidence'] = scan
            if not response['ok']:
                response.pop('result', None)
                response['error']['submission_started'] = False
                response['error']['navigation_started'] = (
                    scan['delivery_started'] is True or scan['activation_started'] is True)
                response['error']['outcome_unknown'] = (
                    response['error'].get('outcome_unknown') is True
                    or not shape_valid
                    or scan['delivery_started'] is not False
                    or scan['activation_started'] is not False
                )
    print(json.dumps(response,ensure_ascii=False),flush=True)

if __name__=='__main__':main()
