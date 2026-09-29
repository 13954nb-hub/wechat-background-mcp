"""Bounded, client-driven polling of already selected conversations.

This module holds no listener process and never sends a message.  The caller
supplies the existing validated public batch reader and keeps the returned
cursor for each room before making another call.
"""

from __future__ import annotations

import copy
import math
import time
from typing import Any, Callable

from mcp.server.fastmcp.exceptions import ToolError

from .batch_read_contract import (
    BatchReadContractError,
    valid_batch_read_result,
    validate_batch_read_request,
)
from .sender_role import unavailable_sender_role


_BATCH_KEYS = frozenset({
    "ok", "status", "verification_level", "account_epoch", "conversations",
    "counts", "coverage", "ordering", "bounded", "full_history", "exact_once",
})
_PUBLIC_KEYS = _BATCH_KEYS | frozenset({
    "multi_database_atomic", "source_consistency", "snapshot_scope",
})
_SPEC_KEYS = frozenset({"conversation_key", "cursor", "limit", "start_from"})


class WatchBatchError(ValueError):
    """A fixed failure code with no callback details or local paths."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _now(monotonic: Callable[[], float], previous: float | None = None) -> float:
    try:
        value = monotonic()
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ValueError
        current = float(value)
        if previous is not None and current < previous:
            raise ValueError
        return current
    except Exception:
        raise WatchBatchError("watch_clock_invalid") from None


def _request(account_epoch: Any, conversations: Any, max_total: Any) -> dict[str, Any]:
    if type(conversations) is not list or not 1 <= len(conversations) <= 16:
        raise WatchBatchError("watch_input_invalid")
    specs: list[dict[str, Any]] = []
    for item in conversations:
        if type(item) is not dict or not set(item) <= _SPEC_KEYS:
            raise WatchBatchError("watch_input_invalid")
        cursor = item.get("cursor")
        if cursor is None:
            raise WatchBatchError("watch_cursor_required")
        if item.get("start_from", "cursor") != "cursor":
            raise WatchBatchError("watch_input_invalid")
        specs.append({
            "conversation_key": item.get("conversation_key"),
            "cursor": cursor,
            "start_from": "cursor",
            "limit": item.get("limit", 16),
        })
    try:
        request = validate_batch_read_request(account_epoch, specs, max_total=max_total)
    except BatchReadContractError:
        raise WatchBatchError("watch_input_invalid") from None
    if any(spec["cursor"]["filter"] != "start_from:now"
           for spec in request["conversations"]):
        raise WatchBatchError("watch_baseline_required")
    return request


def _validated_result(reply: Any, request: dict[str, Any]) -> dict[str, Any]:
    if (type(reply) is not dict or set(reply) != {"ok", "result", "evidence"}
            or reply["ok"] is not True or type(reply["evidence"]) is not dict
            or set(reply["evidence"]) != {"background", "cleanup"}):
        raise WatchBatchError("watch_result_invalid")
    result = reply["result"]
    if type(result) is not dict or set(result) != _PUBLIC_KEYS:
        raise WatchBatchError("watch_result_invalid")
    if (result["multi_database_atomic"] is not False
            or result["source_consistency"] != "observed_quiet_window"
            or result["snapshot_scope"] != "one_authenticated_store_set"):
        raise WatchBatchError("watch_result_invalid")
    canonical = {key: result[key] for key in _BATCH_KEYS}
    if not valid_batch_read_result(canonical, request):
        raise WatchBatchError("watch_result_invalid")
    return reply


def poll_batch_messages(
    *,
    read_batch: Callable[..., dict[str, Any]],
    account_epoch: Any,
    conversations: Any,
    wait_seconds: int,
    max_total: int = 256,
    poll_interval_ms: int = 5000,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Poll 1–16 existing room cursors until activity or the short deadline.

    ``read_batch`` must be the validated ``wechat_batch_read_messages`` public
    callback, called by keyword with account_epoch, conversations and max_total.
    The first cursor for each room must come from its ``start_from=now`` batch
    baseline.  This function never starts a background task or sends replies.
    """

    if not callable(read_batch) or not callable(monotonic) or not callable(sleep):
        raise WatchBatchError("watch_input_invalid")
    if type(wait_seconds) is not int or not 1 <= wait_seconds <= 60:
        raise WatchBatchError("watch_input_invalid")
    if type(poll_interval_ms) is not int or not 1000 <= poll_interval_ms <= 60000:
        raise WatchBatchError("watch_input_invalid")
    request = _request(account_epoch, conversations, max_total)
    started = _now(monotonic)
    previous_time = started
    deadline = started + wait_seconds
    polls = 0
    max_polls = math.ceil(wait_seconds * 1000 / poll_interval_ms) + 1
    while True:
        try:
            reply = read_batch(
                account_epoch=request["account_epoch"],
                conversations=copy.deepcopy(request["conversations"]),
                max_total=request["max_total"],
            )
        except ToolError:
            raise
        except Exception:
            raise WatchBatchError("watch_read_failed") from None
        reply = _validated_result(reply, request)
        polls += 1
        states = reply["result"]["conversations"]
        # Even an empty page can carry a new high-water mark.  Always return
        # the latest room cursor and use it on the following poll.
        request = _request(request["account_epoch"], [
            {"conversation_key": state["conversation_key"],
             "cursor": copy.deepcopy(state["next_cursor"]),
             "limit": old["limit"]}
            for old, state in zip(request["conversations"], states)
        ], request["max_total"])
        observed = _now(monotonic, previous_time)
        previous_time = observed
        if any(state["gap_detected"] for state in states):
            reason = "gap_detected"
        elif reply["result"]["counts"]["items"]:
            reason = "items"
        elif any(state["has_more"] for state in states):
            reason = "has_more"
        elif observed >= deadline or polls >= max_polls:
            reason = "timeout"
        else:
            reason = None
        if reason is None:
            delay = min(poll_interval_ms / 1000, deadline - observed)
            try:
                sleep(delay)
            except Exception:
                raise WatchBatchError("watch_sleep_failed") from None
            observed = _now(monotonic, previous_time)
            previous_time = observed
            if observed < deadline:
                continue
            reason = "timeout"
        result = copy.deepcopy(reply["result"])
        # Normalize legacy internal batches at the public watch boundary.
        # A complete role triple has already passed the batch contract.
        for state in result["conversations"]:
            for item in state["items"]:
                if "sender_role" not in item:
                    item.update(unavailable_sender_role())
        result["watch"] = {
            "mode": "client_driven_long_poll",
            "wake_reason": reason,
            "timed_out": reason == "timeout",
            "polls": polls,
            "elapsed_ms": max(0, int((observed - started) * 1000)),
            "near_real_time_target": True,
            "latency_guaranteed": False,
            "real_time_guaranteed": False,
            "push": False,
            "persistent": False,
            "auto_send": False,
            "poll_interval_ms": poll_interval_ms,
        }
        return {"ok": True, "result": result, "evidence": reply["evidence"]}


__all__ = ["WatchBatchError", "poll_batch_messages"]
