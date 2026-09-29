"""Version-scoped semantic adapter. No SendInput, clipboard, or focus calls."""
from dataclasses import dataclass
import ctypes
import time
import comtypes.client
import psutil
import win32gui
import win32process
from pywinauto import Desktop
from pywinauto.uia_defines import IUIA
from .monitor import visible_windows
from .policy import AdapterError, exact_one, compare_draft, validate_text, validate_ui_action, stable_ref
from .window_geometry import main_client_size_matches_or_minimized


_VOICE_INPUT_SUFFIX = ' 按住 Ctrl + Win 使用語音輸入文字'


def _rectangle(node):
    try:
        value = node.rectangle()
        return value.left, value.top, value.right, value.bottom
    except Exception:
        raise AdapterError('unverified_layout') from None


def _control_point(root, control, *, send=False):
    """Calculate a target-local point from current UIA bounds, never a screen preset."""
    if (len(root) != 4 or len(control) != 4
            or any(type(value) is not int for value in (*root, *control))):
        raise AdapterError('unverified_layout')
    left, top, right, bottom = root
    cl, ct, cr, cb = control
    width, height = right-left, bottom-top
    cw, ch = cr-cl, cb-ct
    if (not 640 <= width <= 32767 or not 480 <= height <= 32767
            or not 12 <= cw <= width-4 or not 12 <= ch <= height-4
            or cl < left+2 or ct < top+2 or cr > right-2 or cb > bottom-2):
        raise AdapterError('unverified_layout')
    x, y = (cl+cr)//2-left, (ct+cb)//2-top
    if send and (cw > min(300,width//3) or ch > min(200,height//3)
                 or x <= width//2 or y <= height//2):
        raise AdapterError('unverified_layout')
    return x, y


@dataclass(frozen=True)
class _SendSnapshot:
    """One immutable membership snapshot and its exact send-control witnesses."""
    nodes: tuple
    sessions: tuple
    target: object
    target_ref: str
    title: str
    selected_refs: tuple
    field: object
    header: str
    button: object | None


class Adapter:
    def __init__(self,target):
        self.pid=int(target['pid']);self.hwnd=int(target['hwnd']);self.created=target['created']
        if psutil.Process(self.pid).create_time()!=self.created:raise AdapterError('stale_process')
        if win32process.GetWindowThreadProcessId(self.hwnd)[1]!=self.pid:raise AdapterError('stale_window')
        uia=IUIA()
        client=comtypes.client.CreateObject(uia.ui_automation_client.CUIAutomation8,interface=uia.ui_automation_client.IUIAutomation2)
        client.AutoSetFocus=False
        client.ConnectionTimeout=3000;client.TransactionTimeout=5000
        if client.AutoSetFocus:raise AdapterError('cannot_disable_auto_focus')
        uia.iuia=client;uia.true_condition=client.CreateTrueCondition();uia.root=client.GetRootElement();uia.get_focused_element=client.GetFocusedElement
        self.client=client;self.identity=f'{self.pid}:{self.created}'
        self.uia_item_container_interface = uia.ui_automation_client.IUIAutomationItemContainerPattern
        self.submission_started=False
        self.precondition()

    def precondition(self):
        if not win32gui.IsIconic(self.hwnd):raise AdapterError('background_requires_minimized','minimize Weixin before using this strict background adapter')
        if win32gui.GetForegroundWindow()==self.hwnd:raise AdapterError('wechat_has_foreground')
        extra=[h for h in visible_windows(self.pid) if h!=self.hwnd]
        if extra:raise AdapterError('existing_popup','close Weixin popovers/dialogs before background operations')

    def root(self):return Desktop(backend='uia').window(handle=self.hwnd).wrapper_object()
    def nodes(self):
        found=self.root().descendants(depth=35)
        if len(found)>2500:raise AdapterError('tree_budget_exceeded')
        return found
    def one(self,predicate,subject):return exact_one([n for n in self.nodes() if predicate(n.element_info)],subject)
    def field(self):return self.one(lambda i:i.automation_id=='chat_input_field','chat_input')
    def current_chat(self):
        fields=[n for n in self.nodes() if n.element_info.automation_id=='chat_input_field']
        if fields:
            name=exact_one(fields,'chat_input').element_info.name
            return name.removesuffix(_VOICE_INPUT_SUFFIX)
        # No open chat is a valid logged-in Chats view only when its complete
        # pinned sidebar and every row's unselected state are observable.
        from .session_navigation import _observe
        _observe(self,None)
        return None
    def ref(self,node,context=''):
        i=node.element_info
        # A session's UIA Name includes its latest preview, timestamp and unread
        # state. These change on send/receive without changing its recipient.
        identity=i.automation_id if i.class_name=='mmui::ChatSessionCell' else i.automation_id+'|'+i.name
        return stable_ref(self.identity,context,str(i.runtime_id),i.class_name,identity)
    def sessions(self,query='',limit=100):
        if not 1<=limit<=100:raise AdapterError('invalid_limit')
        sessions=[]
        for n in self.nodes():
            i=n.element_info
            if i.class_name!='mmui::ChatSessionCell' or not i.automation_id.startswith('session_item_'):continue
            title=i.automation_id[len('session_item_'):]
            if query.casefold() not in title.casefold():continue
            sessions.append({'ref':self.ref(n),'title':title})
        return {'sessions':sessions[:limit],'visible_only':True,'count':len(sessions[:limit]),'current_chat':self.current_chat()}
    def preflight_click(self,node):
        self.precondition()
        # No popup operations: only the already validated root layout is allowed.
        parent=node
        for _ in range(40):
            if parent is None:raise AdapterError('native_parent_missing')
            if parent.handle:break
            parent=parent.parent()
        if parent.handle!=self.hwnd:raise AdapterError('popup_action_unavailable')
        root=self.root().rectangle();rect=node.rectangle()
        _control_point((root.left,root.top,root.right,root.bottom),
                       (rect.left,rect.top,rect.right,rect.bottom))
        renders=[]
        win32gui.EnumChildWindows(self.hwnd,lambda h,_:renders.append(h) if win32gui.GetClassName(h)=='MMUIRenderSubWindowHW' else None,None)
        render=exact_one(renders,'render_surface');client=win32gui.GetClientRect(render)
        if (win32process.GetWindowThreadProcessId(render)[1]!=self.pid
                or tuple(client)!=(0,0,root.width(),root.height())
                or not main_client_size_matches_or_minimized(
                    self.hwnd, (0,0,root.width(),root.height()), win32gui)
                or (not win32gui.IsIconic(self.hwnd)
                    and win32gui.ClientToScreen(self.hwnd,(0,0))!=(root.left,root.top))
                or win32gui.ClientToScreen(self.hwnd,(0,0))
                   != win32gui.ClientToScreen(render,(0,0))):
            raise AdapterError('unverified_geometry')
        x=(rect.left+rect.right)//2-root.left;y=(rect.top+rect.bottom)//2-root.top
        if not(root.left<=rect.left<rect.right<=root.right
                and root.top<=rect.top<rect.bottom<=root.bottom
                and 0<x<root.width() and 0<y<root.height()
                and x<=32767 and y<=32767):raise AdapterError('offscreen_control')
        return (y<<16)|(x&0xffff)

    def click(self,node):
        lp=self.preflight_click(node)
        root_before=_rectangle(self.root())
        target_before=_rectangle(node)
        if (_rectangle(self.root())!=root_before
                or _rectangle(node)!=target_before
                or self.preflight_click(node)!=lp):
            raise AdapterError('unverified_layout')
        self._post_click(lp)

    def click_for_submission(self,node,*,expected_point=None,before_post=None):
        lp=self.preflight_click(node)
        if expected_point is not None:
            x,y=expected_point
            if type(lp) is not int or lp!=((y<<16)|(x&0xffff)):
                raise AdapterError('unverified_layout')
        if before_post is not None:
            before_post()
        self.precondition()
        if self.preflight_click(node)!=lp:
            raise AdapterError('unverified_layout')
        # This is the first point where a target-local input can be posted.
        self.submission_started=True
        try:
            self._post_click(lp)
        except Exception:
            raise AdapterError('outcome_unknown','target-local Send click was attempted; do not automatically resend') from None
    def invoke_for_submission(self,node,session_ref,expected_title,expected_text):
        """Invoke the validated Send button once without coordinate input."""
        self.precondition()
        info=node.element_info
        if (info.control_type!='Button' or info.class_name!='mmui::XOutlineButton'
                or info.name not in ('傳送','发送','發送','Send')):
            raise AdapterError('send_button_unverified')
        try:
            invoke=node.iface_invoke.Invoke
        except Exception:
            raise AdapterError('invoke_unavailable','Send button has no usable InvokePattern') from None
        if not callable(invoke):
            raise AdapterError('invoke_unavailable','Send button has no usable InvokePattern')
        # The draft and target can change between staging and obtaining the
        # provider pattern. Check both again at the final submission boundary.
        if (self.current_chat()!=expected_title or not self.selected_target(session_ref)
                or self.field().iface_value.CurrentValue!=expected_text):
            raise AdapterError('context_conflict','conversation or draft changed before submission')
        self.precondition()
        # Invoke may deliver before the provider reports an error. Once entered,
        # preserve the draft and report unknown; never retry or coordinate-click.
        self.submission_started=True
        try:
            invoke()
        except Exception:
            raise AdapterError('outcome_unknown','UIA Invoke was attempted; do not automatically resend') from None

    def _post_click(self,lp):
        win32gui.PostMessage(self.hwnd,0x201,1,lp)
        try:win32gui.PostMessage(self.hwnd,0x202,0,lp)
        except Exception:
            # Release is cleanup of this same target-local press, not a repeat send.
            win32gui.PostMessage(self.hwnd,0x202,0,lp)
            raise
    def selected_target(self,session_ref):
        sessions=[n for n in self.nodes() if n.element_info.class_name=='mmui::ChatSessionCell']
        matches=[n for n in sessions if self.ref(n)==session_ref]
        if len(matches)!=1:return False
        try:
            selected=[n for n in sessions if bool(n.iface_selection_item.CurrentIsSelected)]
            return len(selected)==1 and self.ref(selected[0])==session_ref
        except Exception:return False

    def _send_snapshot(self,session_ref,require_button=False):
        """Read target, selection, header, field, and optional Send button together."""
        nodes=tuple(self.nodes())
        sessions=tuple(n for n in nodes if n.element_info.class_name=='mmui::ChatSessionCell')
        target=exact_one([n for n in sessions if self.ref(n)==session_ref],'session_ref')
        automation_id=target.element_info.automation_id
        title=automation_id.removeprefix('session_item_')
        if len([n for n in sessions if n.element_info.automation_id=='session_item_'+title])!=1:
            raise AdapterError('ambiguous_recipient','duplicate display names are unsupported; nothing was sent')
        field=exact_one([n for n in nodes if n.element_info.automation_id=='chat_input_field'],'chat_input')
        buttons=[n for n in nodes if n.element_info.control_type=='Button'
                 and n.element_info.class_name=='mmui::XOutlineButton'
                 and n.element_info.name in ('傳送','发送','發送','Send')]
        button=exact_one(buttons,'send_button') if require_button else (buttons[0] if len(buttons)==1 else None)
        selected_refs=self._selected_session_refs(sessions)
        return _SendSnapshot(
            nodes=nodes, sessions=sessions, target=target, target_ref=self.ref(target),
            title=title, selected_refs=selected_refs, field=field,
            header=field.element_info.name, button=button,
        )

    def _selected_session_refs(self,sessions):
        try:
            return tuple(self.ref(n) for n in sessions
                         if bool(n.iface_selection_item.CurrentIsSelected))
        except Exception:
            return ()

    def _snapshot_matches_target(self,snapshot,session_ref,title):
        try:
            return (snapshot.target_ref==session_ref and self.ref(snapshot.target)==session_ref
                    and snapshot.title==title
                    and snapshot.header in (title,title+_VOICE_INPUT_SUFFIX)
                    and snapshot.field.element_info.name==snapshot.header
                    and snapshot.selected_refs==(session_ref,)
                    and self._selected_session_refs(snapshot.sessions)==(session_ref,))
        except Exception:
            return False

    @staticmethod
    def _field_runtime_id(field):
        """Return a comparable UIA RuntimeId, or None when identity is unproven."""
        try:
            runtime_id=field.element_info.runtime_id
            if isinstance(runtime_id,str):
                identity=(runtime_id,)
            else:
                identity=tuple(runtime_id)
            return identity or None
        except Exception:
            return None

    def _field_identity(self,field,context):
        runtime_id=self._field_runtime_id(field)
        if not runtime_id:
            return None
        try:
            return runtime_id,self.ref(field,context)
        except Exception:
            return None

    def _click_send_snapshot(self,snapshot,session_ref,expected_title,expected_text):
        """Submit once through the measured semantic Send control."""
        if (not self._snapshot_matches_target(snapshot,session_ref,expected_title)
                or snapshot.field.iface_value.CurrentValue!=expected_text):
            raise AdapterError('draft_write_unverified','conversation, selection, or staged text changed before submission')
        button=snapshot.button
        info=button.element_info
        if (info.control_type!='Button' or info.class_name!='mmui::XOutlineButton'
                or info.name not in ('傳送','发送','發送','Send')):
            raise AdapterError('send_button_unverified')
        root_rect=_rectangle(self.root())
        button_rect=_rectangle(button)
        point=_control_point(root_rect,button_rect,send=True)

        def recheck():
            info=button.element_info
            if (info.control_type!='Button' or info.class_name!='mmui::XOutlineButton'
                    or info.name not in ('傳送','发送','發送','Send')):
                raise AdapterError('send_button_unverified')
            if _rectangle(self.root())!=root_rect or _rectangle(button)!=button_rect:
                raise AdapterError('unverified_layout')
            if (not self._snapshot_matches_target(snapshot,session_ref,expected_title)
                    or snapshot.field.iface_value.CurrentValue!=expected_text):
                raise AdapterError('context_conflict','conversation or draft changed before submission')

        # preflight_click verifies the unique render surface and computes the
        # target-local point. Recheck mutable witnesses after that provider work.
        self.click_for_submission(button,expected_point=point,before_post=recheck)

    def _clear_owned_send_draft(self,session_ref,title,text,original_field,original_identity):
        """Clear only a still-matching operation-owned draft; ambiguity preserves it."""
        if original_identity is None:
            return
        try:
            if self._field_identity(original_field,title)!=original_identity:
                return
            snapshot=self._send_snapshot(session_ref)
            if (self._field_identity(snapshot.field,title)==original_identity
                    and self._snapshot_matches_target(snapshot,session_ref,title)
                    and snapshot.field.iface_value.CurrentValue==text
                    and original_field.iface_value.CurrentValue==text):
                # Write only through the exact wrapper that received SetValue;
                # the snapshot proves it is still the visible field.
                original_field.iface_value.SetValue('')
        except Exception:
            # A missing or ambiguous fresh view cannot authorize clearing text.
            return

    def open_session(self,session_ref):
        navigation={'session_ref':session_ref if type(session_ref) is str else None,
                    'title':None,'activation_started':False,
                    'selected_and_header_verified':False}
        self.navigation_evidence=navigation
        original_chat=self.current_chat()
        if original_chat is None:
            from .session_navigation import _observe
            from .observed_adapter import rectangle
            view=_observe(self,None)
            all_sessions=list(view['all_rows'])
        else:
            all_sessions=[n for n in self.nodes() if n.element_info.class_name=='mmui::ChatSessionCell']
        candidates=[n for n in all_sessions if self.ref(n)==session_ref]
        node=exact_one(candidates,'session_ref')
        title=node.element_info.automation_id.removeprefix('session_item_')
        if len([n for n in all_sessions if n.element_info.automation_id=='session_item_'+title])!=1:
            raise AdapterError('ambiguous_recipient','duplicate display names are unsupported; nothing was sent')
        navigation['title']=title
        validate_ui_action([title],'ListItem')
        # An already selected, exact ref plus its matching header is sufficient.
        # Re-clicking a selected Qt item can toggle it off. A matching title
        # alone is never sufficient: other refs must still be navigated to.
        already_open=(original_chat is not None
                      and self.selected_target(session_ref) and self.current_chat()==title)
        if not already_open:
            if original_chat is None:
                # Reobserve immediately before activation. A stale, clipped
                # or ambiguously named UIA row cannot authorize a click.
                view=_observe(self,None)
                fresh=[row for row in view['all_rows'] if self.ref(row)==session_ref]
                node=exact_one(fresh,'session_ref')
                if (node.element_info.automation_id!='session_item_'+title
                        or len([row for row in view['all_rows']
                                if row.element_info.automation_id=='session_item_'+title])!=1):
                    raise AdapterError('context_conflict')
                bounds=rectangle(node)
                table_rect=view['table_rect']
                if (node not in view['rows'] or bounds[1]<table_rect[1]
                        or bounds[3]>table_rect[3]):
                    raise AdapterError('offscreen_control')
            navigation['activation_started']=True
            self.click(node)
            for _ in range(6):
                time.sleep(.1)
                if self.selected_target(session_ref) and self.current_chat()==title:break
            else:raise AdapterError('session_not_opened','target row selection and conversation header did not agree')
        if original_chat is None:
            field=self.field()
            info=field.element_info
            if (info.class_name!='mmui::ChatInputField' or info.control_type!='Edit'
                    or info.name!=title or type(field.iface_value.CurrentValue) is not str
                    or not self.selected_target(session_ref)):
                raise AdapterError('session_not_opened')
        navigation['selected_and_header_verified']=True
        return {'title':title,'status':'opened','verification_level':'client_ui','background_mode':'minimized'}
    def messages(self,limit=50):
        if not 1<=limit<=200:raise AdapterError('invalid_limit')
        chat=self.current_chat()
        validate_ui_action([chat],'ListItem')
        from .history_view import _is_direct
        from .sender_role import classify_ui_center, unavailable_sender_role
        nodes=self.nodes()
        frame=exact_one([n for n in nodes
                         if n.element_info.class_name=='mmui::MessageView'
                         and n.element_info.control_type=='Group'],'message_frame')
        listing=exact_one([n for n in nodes
                           if n.element_info.class_name=='mmui::RecyclerListView'
                           and n.element_info.control_type=='List'
                           and _is_direct(n,frame,'mmui::MessageView','Group')],
                          'message_list')
        try:
            root_rect=_rectangle(self.root())
            frame_rect=_rectangle(frame)
            left,top,right,bottom=frame_rect
            geometry_ok=(root_rect[0]<=left<right<=root_rect[2]
                         and root_rect[1]<=top<bottom<=root_rect[3])
        except Exception:
            # Sender classification is optional evidence. If the window or
            # frame geometry cannot be read, still return the visible message
            # text and mark its sender as unavailable.
            frame_rect=None
            geometry_ok=False

        def runtime_id(node):
            try:
                value=node.element_info.runtime_id
                return None if value is None or value=='' or value==() or value==[] else str(value)
            except Exception:
                return None

        def belongs_to_row(node,row_runtime):
            current=node
            for _ in range(41):
                if current is None:return False
                if runtime_id(current)==row_runtime:return True
                try:current=current.parent()
                except Exception:return False
            return False

        def row_role(row):
            if not geometry_ok or frame_rect is None:
                return unavailable_sender_role()
            row_runtime=runtime_id(row)
            if row_runtime is None:return unavailable_sender_role()
            text=row.element_info.name
            candidates=[node for node in nodes
                        if node.element_info.control_type=='Text'
                        and node.element_info.name==text
                        and belongs_to_row(node,row_runtime)]
            if not candidates:return unavailable_sender_role()
            if len(candidates)!=1:
                from .sender_role import sender_role_fields
                return sender_role_fields('unknown','ambiguous')
            try:bounds=_rectangle(candidates[0])
            except AdapterError:return unavailable_sender_role()
            if not (left<=bounds[0]<bounds[2]<=right
                    and top<=bounds[1]<bounds[3]<=bottom):
                return unavailable_sender_role()
            return classify_ui_center((bounds[0]+bounds[2])/2,left,right-left)

        messages=[]
        for n in nodes:
            i=n.element_info
            if (i.class_name.startswith('mmui::Chat')
                    and i.class_name.endswith('ItemView')
                    and _is_direct(n,listing,'mmui::RecyclerListView','List')):
                if i.control_type!='ListItem' or type(i.name) is not str:
                    raise AdapterError('history_row_unsupported')
                role_fields=row_role(n)
                messages.append({'ref':self.ref(n,chat),'type':i.class_name,'text':i.name,
                                 **role_fields})
        if geometry_ok:
            try:
                if (_rectangle(frame)!=frame_rect or _rectangle(self.root())!=root_rect):
                    from .sender_role import sender_role_fields
                    messages=[{**message,**sender_role_fields('unknown','ambiguous')}
                              for message in messages]
            except Exception:
                messages=[{**message,**unavailable_sender_role()} for message in messages]
        if self.current_chat()!=chat:raise AdapterError('context_conflict')
        return {'chat':chat,'messages':messages[-limit:],'visible_only':True,'not_full_history':True}
    def draft(self):
        field=self.field();return {'chat':field.element_info.name,'text':field.iface_value.CurrentValue}
    def set_draft(self,session_ref,text,expected_text):
        validate_text(text,allow_empty=True)
        self.open_session(session_ref)
        field=self.field();compare_draft(field.iface_value.CurrentValue,expected_text)
        if not self.selected_target(session_ref):raise AdapterError('context_conflict')
        field.iface_value.SetValue(text)
        if not self.selected_target(session_ref) or field.iface_value.CurrentValue!=text:raise AdapterError('draft_write_unverified')
        return {'status':'draft_updated','verification_level':'client_readback','counts':{'characters':len(text)},'background_mode':'minimized','ok':True}
    def send_text(self,session_ref,text):
        validate_text(text)
        snapshot=self._send_snapshot(session_ref)
        title=snapshot.title
        validate_ui_action([title],'ListItem')
        if not self._snapshot_matches_target(snapshot,session_ref,title):
            self.click(snapshot.target)
            for _ in range(6):
                time.sleep(.1)
                snapshot=self._send_snapshot(session_ref)
                if self._snapshot_matches_target(snapshot,session_ref,title):break
            else:
                raise AdapterError('session_not_opened','target row selection and conversation header did not agree')
        field=snapshot.field
        field_identity=self._field_identity(field,title)
        compare_draft(field.iface_value.CurrentValue,'')
        before={str(n.element_info.runtime_id) for n in snapshot.nodes if n.element_info.class_name=='mmui::ChatTextItemView'}
        try:
            field.iface_value.SetValue(text)
            self.precondition()
            final_snapshot=self._send_snapshot(session_ref,require_button=True)
            # Preflight finishes before the first possible input message;
            # failures there still clear only the operation-owned draft.
            self._click_send_snapshot(final_snapshot,session_ref,title,text)
            for _ in range(8):
                time.sleep(.2)
                try:
                    post_snapshot=self._send_snapshot(session_ref)
                    target_still_matches=self._snapshot_matches_target(post_snapshot,session_ref,title)
                    matching=[n for n in post_snapshot.nodes
                              if n.element_info.class_name=='mmui::ChatTextItemView'
                              and n.element_info.name==text
                              and str(n.element_info.runtime_id) not in before]
                    draft_empty=not post_snapshot.field.iface_value.CurrentValue
                    target_still_matches=(target_still_matches
                                          and self._snapshot_matches_target(post_snapshot,session_ref,title))
                    message_ref=self.ref(matching[-1],title) if matching and draft_empty else None
                except Exception:
                    raise AdapterError('outcome_unknown','post-submission verification failed; do not resend') from None
                if not target_still_matches:
                    raise AdapterError('outcome_unknown','conversation changed after submission; do not resend')
                if message_ref is not None:
                    return {'ok':True,'status':'submitted','verification_level':'new_local_bubble_and_empty_draft','counts':{'submitted':1},'refs':{'message':message_ref},'background_mode':'minimized'}
            raise AdapterError('outcome_unknown','submission attempted; do not automatically resend')
        finally:
            if not self.submission_started:
                # Restore only a draft this operation still owns.
                self._clear_owned_send_draft(session_ref,title,text,field,field_identity)

    def send_at_username(self,session_ref,username,text=''):
        from .mention_text import build_at_username_text
        return self.send_text(session_ref,build_at_username_text(username,text))

    def layout_calibration(self):
        """Read this machine's live UIA/native mapping; never persist coordinates."""
        self.precondition()
        root=self.root()
        info=root.element_info
        root_rect=_rectangle(root)
        if info.class_name!='mmui::MainWindow' or info.control_type!='Window':
            raise AdapterError('unverified_layout')
        left,top,right,bottom=root_rect
        if not 640<=right-left<=32767 or not 480<=bottom-top<=32767:
            raise AdapterError('unverified_layout')
        renders=[]
        win32gui.EnumChildWindows(self.hwnd,lambda h,_:renders.append(h)
            if win32gui.GetClassName(h)=='MMUIRenderSubWindowHW' else None,None)
        render=exact_one(renders,'render_surface')
        client=tuple(win32gui.GetClientRect(render))
        if (win32process.GetWindowThreadProcessId(render)[1]!=self.pid
                or client!=(0,0,right-left,bottom-top)
                or not main_client_size_matches_or_minimized(
                    self.hwnd, (0,0,right-left,bottom-top), win32gui)
                or (not win32gui.IsIconic(self.hwnd)
                    and win32gui.ClientToScreen(self.hwnd,(0,0))!=(left,top))
                or win32gui.ClientToScreen(self.hwnd,(0,0))
                   !=win32gui.ClientToScreen(render,(0,0))):
            raise AdapterError('unverified_geometry')
        dpi=int(ctypes.windll.user32.GetDpiForWindow(self.hwnd))
        if not 72<=dpi<=768:
            raise AdapterError('coordinate_context_unverified')
        nodes=tuple(self.nodes())

        def observed(predicate,*,send=False):
            matching=[node for node in nodes if predicate(node)]
            if len(matching)!=1:
                return None
            try:
                bounds=_rectangle(matching[0])
                point=_control_point(root_rect,bounds,send=send)
            except AdapterError:
                return None
            return {'rect':list(bounds),'client_point':list(point)}

        def session_table(node):
            info=node.element_info
            if info.class_name!='mmui::XTableView' or info.control_type!='List':
                return False
            parent=node.parent()
            return (parent is not None
                    and parent.element_info.class_name=='mmui::ChatSessionList'
                    and parent.element_info.control_type=='Group')

        return {'root_rect':list(root_rect),'render_client':list(client),
                'window_dpi':dpi,'coordinate_mapping_verified':True,
                'send_button':observed(lambda n:n.element_info.control_type=='Button'
                    and n.element_info.class_name=='mmui::XOutlineButton'
                    and n.element_info.name in ('傳送','发送','發送','Send'),send=True),
                'attachment_button':observed(lambda n:n.element_info.control_type=='Button'
                    and n.element_info.class_name=='mmui::XButton'
                    and n.element_info.name=='傳送檔案'),
                'session_table':observed(session_table)}

    def dispatch(self,action,args):
        if action=='list_sessions':return self.sessions(**args)
        if action=='probe_session_container':
            from .session_locator import probe_item_container
            return probe_item_container(self, **args)
        if action=='list_contacts':
            from .contact_actions import list_contacts
            return list_contacts(self,**args)
        if action=='open_session':return self.open_session(**args)
        if action=='scan_open_session':
            from .session_navigation import scan_open_session
            return scan_open_session(self, **args)
        if action=='read_messages':return self.messages(**args)
        if action=='get_draft':return self.draft()
        if action=='set_draft':return self.set_draft(**args)
        if action=='send_text':return self.send_text(**args)
        if action=='send_at_username':return self.send_at_username(**args)
        if action=='send_file':
            from .attachments import send_file
            return send_file(self,**args)
        if action=='status':return {'status':'ready','pid':self.pid,'current_chat':self.current_chat(),'strict_background':True,'auto_set_focus':False,'account_identity_verified':False,'layout_calibration':self.layout_calibration()}
        raise AdapterError('unsupported_action')
