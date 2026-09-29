"""Provider seam for a bounded multi-conversation read.

The authenticated read-store adapter supplies the per-conversation callback.
This module validates each projected room and assembles the public batch; it
does not open SQLite, touch UIA, or start a listener.
"""

from __future__ import annotations

from typing import Any, Callable

from .batch_read_contract import (
    BatchReadContractError,
    valid_batch_read_result,
    validate_batch_read_request,
)


_SINGLE_KEYS = frozenset(
    (
        "account_epoch",
        "items",
        "count",
        "has_more",
        "next_cursor",
        "gap_detected",
        "bounded",
        "full_history",
        "exact_once",
        "coverage",
        "ordering",
    )
)


class BatchReadProviderError(RuntimeError):
    """Fixed public-safe provider failure code."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _single_result(value: Any, *, account_epoch: Any, conversation_key: str, limit: int) -> dict[str, Any]:
    if type(value) is not dict or set(value) != _SINGLE_KEYS:
        raise BatchReadProviderError("batch_read_result_invalid")
    if value["account_epoch"] != account_epoch:
        raise BatchReadProviderError("batch_read_account_changed")
    if type(value["items"]) is not list or len(value["items"]) > limit:
        raise BatchReadProviderError("batch_read_result_invalid")
    if type(value["count"]) is not int or value["count"] != len(value["items"]):
        raise BatchReadProviderError("batch_read_result_invalid")
    if type(value["has_more"]) is not bool or type(value["gap_detected"]) is not bool:
        raise BatchReadProviderError("batch_read_result_invalid")
    if value["bounded"] is not True or value["full_history"] is not False or value["exact_once"] is not False:
        raise BatchReadProviderError("batch_read_result_invalid")
    if value["coverage"] != "bounded_message_table_query" or value["ordering"] != "rowid_continuation_not_chronological":
        raise BatchReadProviderError("batch_read_result_invalid")
    return {
        "conversation_key": conversation_key,
        "items": list(value["items"]),
        "next_cursor": value["next_cursor"],
        "has_more": value["has_more"],
        "gap_detected": value["gap_detected"],
    }


class BatchReadProvider:
    """Call one already-authenticated reader per exact conversation."""

    __slots__ = ("_read_one",)

    def __init__(self, *, read_one: Callable[[Any, dict[str, Any]], dict[str, Any]]):
        if not callable(read_one):
            raise TypeError("read_one must be callable")
        self._read_one = read_one

    def read(self, *, account_epoch: Any, conversations: Any, max_total: Any = 256) -> dict[str, Any]:
        request = validate_batch_read_request(
            account_epoch, conversations, max_total=max_total
        )
        states: list[dict[str, Any]] = []
        try:
            for spec in request["conversations"]:
                try:
                    value = self._read_one(request["account_epoch"], dict(spec))
                except BatchReadProviderError:
                    raise
                except Exception:
                    raise BatchReadProviderError("batch_read_unavailable") from None
                states.append(
                    _single_result(
                        value,
                        account_epoch=request["account_epoch"],
                        conversation_key=spec["conversation_key"],
                        limit=spec["limit"],
                    )
                )
        except BatchReadProviderError:
            raise

        result = {
            "ok": True,
            "status": "partial"
            if any(item["has_more"] or item["gap_detected"] for item in states)
            else "complete",
            "verification_level": "authenticated_readstore_batch",
            "account_epoch": request["account_epoch"],
            "conversations": states,
            "counts": {
                "conversations": len(states),
                "items": sum(len(item["items"]) for item in states),
            },
            "coverage": "bounded_authenticated_batch",
            "ordering": "rowid_continuation_not_chronological",
            "bounded": True,
            "full_history": False,
            "exact_once": False,
        }
        if not valid_batch_read_result(result, request):
            raise BatchReadProviderError("batch_read_result_invalid")
        return result


__all__ = ["BatchReadProvider", "BatchReadProviderError"]
