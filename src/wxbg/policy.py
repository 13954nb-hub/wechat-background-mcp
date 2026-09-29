"""Pure preconditions shared by the Windows adapter and its tests."""
import hashlib
import json

class AdapterError(RuntimeError):
    def __init__(self, code, detail=''):
        self.code=code
        super().__init__(code + (': '+detail if detail else ''))

def exact_one(items, subject):
    if not items:raise AdapterError('not_found',subject)
    if len(items)!=1:raise AdapterError('ambiguous',subject)
    return items[0]

def compare_draft(actual, expected):
    if actual!=expected:raise AdapterError('draft_conflict','existing draft was preserved')

def validate_text(text, allow_empty=False):
    if not isinstance(text,str) or '\0' in text or len(text)>10000 or (not allow_empty and not text.strip()):
        raise AdapterError('invalid_text','requires 1-10000 characters, without NUL')
    return text

PAYMENT_NAMES=('支付','紅包','红包','轉帳','转账','轉賬','转账','收款','付款','錢包','钱包','wechat pay','payment','red packet','money transfer')

def validate_ui_action(ancestors, control_type):
    names=[n.strip().lower() for n in ancestors if n.strip()]
    if any(keyword in n for n in names for keyword in PAYMENT_NAMES):raise AdapterError('payment_excluded')
    if not names or control_type not in ('Button','ListItem','TabItem'):
        raise AdapterError('unsupported_ui_action')

def stable_ref(process, context, runtime, kind, name):
    value=json.dumps([process,context,runtime,kind,name],ensure_ascii=False,separators=(',',':'))
    return hashlib.sha256(value.encode()).hexdigest()[:32]
