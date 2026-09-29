"""Pure parser and public-result contract for local Weixin file cards.

This module deliberately has no UI, native, Weixin, MCP, or ETW dependency.
The filename is retained only in the internal parser result; callers must use
``valid_file_card_result`` for the redacted public result contract.
"""

import re

from wxbg.policy import AdapterError


FILE_CARD_LAYOUT_ERROR = "file_card_layout_unverified"
FILE_CARD_READ_ERROR = "file_card_read_failed"
_SAFE_FILE_CARD_ERRORS = frozenset(
    (
        "invalid_session_ref",
        "invalid_message_ref",
        "invalid_deadline",
        "file_card_budget_exhausted",
        "context_conflict",
        "draft_conflict",
        "file_card_not_found",
        "ambiguous_file_card",
        "file_card_changed",
        "file_card_ref_stale",
        FILE_CARD_LAYOUT_ERROR,
        FILE_CARD_READ_ERROR,
    )
)

_MAX_CARD_TEXT = 4096
_MAX_LINE_LENGTH = 255
_REF_RE = re.compile(r"[0-9a-f]{32}\Z")
_PROGRESS_RE = re.compile(r"(?:进度|進度)[ \t]*:[ \t]*([0-9]{1,3})[ \t]*%\Z")
_MALFORMED_PROGRESS_RE = re.compile(
    r"(?:进度|進度)(?:[ \t]*[:：][ \t]*[+\-\d.% \t]*|[ \t]+[+\-\d.% \t]+)\Z"
)
_SIZE_RE = re.compile(r"((?:0|[1-9][0-9]{0,9})(?:\.[0-9]{1,2})?)[ \t]*(B|K|KB|MB|GB)\Z")

# These are the complete labels used by the existing file_actions._upload_status
# helper.  They are copied here to keep this pure contract module independent
# from the native/UI path and to preserve the old helper unchanged.
_STATUS_LABELS = {
    "interrupted": frozenset(
        (
            "傳送中斷",
            "发送中断",
            "傳輸中斷",
            "传输中断",
            "上傳中斷",
            "上传中断",
        )
    ),
    "failed": frozenset(
        (
            "傳送失敗",
            "发送失败",
            "傳輸失敗",
            "传输失败",
            "上傳失敗",
            "上传失败",
        )
    ),
    "completed_label": frozenset(
        (
            "上傳完成",
            "上传完成",
            "傳送完成",
            "发送完成",
            "傳送成功",
            "发送成功",
        )
    ),
    "uploading": frozenset(
        (
            "正在上傳",
            "正在上传",
            "正在傳送",
            "正在发送",
        )
    ),
}
_STATUS_BY_LABEL = {
    label: status for status, labels in _STATUS_LABELS.items() for label in labels
}

_PUBLIC_RESULT_KEYS = frozenset(
    (
        "ok",
        "status",
        "verification_level",
        "background_mode",
        "transfer_indicator",
        "progress_percent",
        "upload_status",
        "display_size",
        "remote_receipt_verified",
        "stable_message_id",
        "not_full_history",
        "counts",
        "refs",
    )
)
_TRANSFER_INDICATORS = frozenset(
    (
        "uploading",
        "interrupted",
        "failed",
        "completed_label",
        "no_transfer_indicator",
        "unknown",
    )
)
_UPLOAD_STATUSES = frozenset(("uploading", "interrupted", "failed", "completed", "unknown"))
_UPLOAD_STATUS_BY_INDICATOR = {
    "uploading": "uploading",
    "interrupted": "interrupted",
    "failed": "failed",
    "completed_label": "completed",
    "no_transfer_indicator": "unknown",
    "unknown": "unknown",
}


def _layout_error():
    raise AdapterError(FILE_CARD_LAYOUT_ERROR)


def _normalised_size(value):
    if type(value) is not str or not value or len(value) > 32:
        return None
    match = _SIZE_RE.fullmatch(value)
    if match is None:
        return None
    return match.group(1) + match.group(2)


def _valid_ref(value):
    return type(value) is str and _REF_RE.fullmatch(value) is not None


def _normalise_card_lines(text):
    if type(text) is not str or "\x00" in text or len(text) > _MAX_CARD_TEXT:
        _layout_error()
    normalised = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalised.split("\n")
    if (
        len(lines) < 4
        or lines[0] != "檔案"
        or lines[-1] != "微信电脑版"
        or any(len(line) > _MAX_LINE_LENGTH for line in lines)
        or any(not line for line in lines[1:-1])
    ):
        _layout_error()
    return lines


def parse_file_card(text: str) -> dict:
    """Parse the bounded local card grammar into an internal exact shape."""

    lines = _normalise_card_lines(text)
    body_end = len(lines) - 1

    progress_value = None
    cursor = 1
    first_body_line = lines[cursor]
    progress_match = _PROGRESS_RE.fullmatch(first_body_line)
    if progress_match is not None:
        progress_value = int(progress_match.group(1))
        if progress_value > 100:
            _layout_error()
        cursor += 1
    elif _MALFORMED_PROGRESS_RE.fullmatch(first_body_line) is not None:
        # A malformed progress-looking line must not become a filename.
        _layout_error()

    remaining = lines[cursor:body_end]
    if len(remaining) not in (2, 3):
        _layout_error()

    filename = remaining[0]
    if (
        not filename.strip()
        or len(filename) > _MAX_LINE_LENGTH
        or any(ord(character) < 32 for character in filename)
    ):
        _layout_error()

    display_size = _normalised_size(remaining[1])
    if display_size is None:
        _layout_error()

    status_label = remaining[2].strip() if len(remaining) == 3 else None
    if len(remaining) == 3 and not status_label:
        _layout_error()
    if status_label is not None and (
        _PROGRESS_RE.fullmatch(status_label) is not None
        or _MALFORMED_PROGRESS_RE.fullmatch(status_label) is not None
    ):
        # Progress belongs before the filename and may occur only once.
        _layout_error()
    status = _STATUS_BY_LABEL.get(status_label) if status_label is not None else None

    if status is None:
        if status_label is not None:
            transfer_indicator = "unknown"
            upload_status = "unknown"
        elif progress_value is not None:
            transfer_indicator = "uploading"
            upload_status = "uploading"
        else:
            transfer_indicator = "no_transfer_indicator"
            upload_status = "unknown"
    elif status == "completed_label" and progress_value is not None and progress_value != 100:
        # A completion label cannot override an observed incomplete progress.
        transfer_indicator = "unknown"
        upload_status = "unknown"
    else:
        transfer_indicator = status
        upload_status = _UPLOAD_STATUS_BY_INDICATOR[status]

    return {
        "filename": filename,
        "display_size": display_size,
        "transfer_indicator": transfer_indicator,
        "progress_percent": progress_value,
        "upload_status": upload_status,
    }


def valid_file_card_result(result, session_ref=None, message_ref=None) -> bool:
    """Return whether *result* is exactly the redacted public result contract."""

    if type(result) is not dict or set(result) != _PUBLIC_RESULT_KEYS:
        return False

    if result["ok"] is not True or type(result["ok"]) is not bool:
        return False
    if result["status"] != "observed" or type(result["status"]) is not str:
        return False
    if (
        result["verification_level"] != "two_matching_local_file_card_observations"
        or type(result["verification_level"]) is not str
    ):
        return False
    if result["background_mode"] != "minimized" or type(result["background_mode"]) is not str:
        return False

    transfer_indicator = result["transfer_indicator"]
    upload_status = result["upload_status"]
    if type(transfer_indicator) is not str or transfer_indicator not in _TRANSFER_INDICATORS:
        return False
    if type(upload_status) is not str or upload_status not in _UPLOAD_STATUSES:
        return False
    if _UPLOAD_STATUS_BY_INDICATOR[transfer_indicator] != upload_status:
        return False

    progress_percent = result["progress_percent"]
    if progress_percent is not None and (
        type(progress_percent) is not int or not 0 <= progress_percent <= 100
    ):
        return False
    if transfer_indicator == "no_transfer_indicator" and progress_percent is not None:
        return False
    if transfer_indicator == "completed_label" and progress_percent not in (None, 100):
        return False

    display_size = result["display_size"]
    if type(display_size) is not str or _normalised_size(display_size) != display_size:
        return False
    if result["remote_receipt_verified"] is not False or type(result["remote_receipt_verified"]) is not bool:
        return False
    if result["stable_message_id"] is not False or type(result["stable_message_id"]) is not bool:
        return False
    if result["not_full_history"] is not True or type(result["not_full_history"]) is not bool:
        return False

    counts = result["counts"]
    if type(counts) is not dict or set(counts) != {"observations"}:
        return False
    if type(counts["observations"]) is not int or counts["observations"] != 2:
        return False

    refs = result["refs"]
    if type(refs) is not dict or set(refs) != {"conversation", "message"}:
        return False
    if not _valid_ref(refs["conversation"]) or not _valid_ref(refs["message"]):
        return False
    if session_ref is not None and (
        not _valid_ref(session_ref) or refs["conversation"] != session_ref
    ):
        return False
    if message_ref is not None and (
        not _valid_ref(message_ref) or refs["message"] != message_ref
    ):
        return False
    return True


def safe_file_card_error(code) -> str:
    """Return only a fixed public file-card error code."""

    if type(code) is str and code in _SAFE_FILE_CARD_ERRORS:
        return code
    return FILE_CARD_READ_ERROR
