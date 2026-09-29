"""Authenticated, bounded batch reads over one read-store quiet window."""

from __future__ import annotations

import json
import math
import time
from typing import Any, Callable

from mcp.server.fastmcp.exceptions import ToolError

from .batch_read_contract import (
    BatchReadContractError, valid_batch_read_result, validate_batch_read_request,
)
from .batch_read_provider import BatchReadProvider, BatchReadProviderError
from .readstore_message_identity import IdentityError, resolve_conversation
from .readstore_query import IncrementalCursor, ReadStoreError, ReadStoreQuery
from .readstore_search_public import message_row
from .readstore_batch_public import MAX_BATCH_PUBLIC_BYTES
from .readstore_provider import ProviderError
from .worker import MAX_READSTORE_RESULT_BYTES


_ARG_KEYS = frozenset(("account_epoch", "conversations", "max_total"))
_QUERY_KEYS = frozenset(
    (
        "items",
        "count",
        "has_more",
        "next_cursor",
        "bounded",
        "full_history",
        "exact_once",
        "gap_detected",
        "ordering",
        "coverage",
    )
)


def _check_deadline(deadline: Any) -> None:
    if type(deadline) not in (int, float) or not math.isfinite(deadline):
        raise ProviderError("readstore_invalid_deadline")
    if time.monotonic() >= deadline:
        raise ProviderError("readstore_deadline")


def _validated_request(args: Any) -> dict[str, Any]:
    if type(args) is not dict or set(args) != _ARG_KEYS:
        raise ProviderError("readstore_input_invalid")
    try:
        return validate_batch_read_request(
            args["account_epoch"],
            args["conversations"],
            max_total=args["max_total"],
        )
    except BatchReadContractError:
        raise ProviderError("readstore_input_invalid") from None


def _single_query_result(
    raw: Any,
    *,
    conversation_key: str,
    chat_md5: str,
    account_epoch: str | int,
    shard_ids: list[str],
    limit: int,
) -> dict[str, Any]:
    if type(raw) is not dict or not _QUERY_KEYS <= set(raw):
        raise ProviderError("readstore_result_invalid")
    if (
        raw["bounded"] is not True
        or raw["full_history"] is not False
        or raw["exact_once"] is not False
        or raw["coverage"] != "bounded_message_table_query"
        or raw["ordering"] != "rowid_asc_per_shard_merge"
        or type(raw["items"]) is not list
        or len(raw["items"]) > limit
        or type(raw["count"]) is not int
        or raw["count"] != len(raw["items"])
    ):
        raise ProviderError("readstore_result_invalid")

    rows = []
    identities = set()
    for raw_row in raw["items"]:
        try:
            row = message_row(raw_row, chat_md5, shard_ids)
        except ToolError:
            raise ProviderError("readstore_result_invalid") from None
        identity_key = (row["shard_id"], row["message_identity"]["rowid"])
        if identity_key in identities:
            raise ProviderError("readstore_result_invalid")
        identities.add(identity_key)
        rows.append(
            {
                "conversation_key": conversation_key,
                "account_epoch": account_epoch,
                **row,
            }
        )

    return {
        "account_epoch": account_epoch,
        "items": rows,
        "count": raw["count"],
        "has_more": raw["has_more"],
        "next_cursor": raw["next_cursor"],
        "gap_detected": raw["gap_detected"],
        "bounded": True,
        "full_history": False,
        "exact_once": False,
        "coverage": "bounded_message_table_query",
        "ordering": "rowid_continuation_not_chronological",
    }


def _cursor_for_prefix(
    spec: dict[str, Any], state: dict[str, Any], rows: list[dict[str, Any]]
) -> dict[str, Any]:
    """Advance each shard only through rows actually included in this batch."""
    observed = IncrementalCursor.from_value(state["next_cursor"])
    prior = (
        IncrementalCursor.from_value(spec["cursor"])
        if spec["cursor"] is not None else None
    )
    highwater = (
        dict(prior.shard_highwater)
        if prior is not None else {shard_id: None for shard_id in observed.shard_ids}
    )
    for row in rows:
        shard_id = row["shard_id"]
        candidate = (
            row["sort_seq"], row["local_id"], row["message_identity"]["rowid"]
        )
        old = highwater[shard_id]
        if old is None or candidate[2] > old[2]:
            highwater[shard_id] = candidate
    positions = tuple((shard_id, highwater[shard_id]) for shard_id in observed.shard_ids)
    return IncrementalCursor(
        observed.chat_md5, observed.filter_key, observed.shard_ids,
        observed.account_epoch, positions, positions, state["gap_detected"]
    ).as_dict()


def _prefix_page(
    batch: dict[str, Any], request: dict[str, Any], counts: list[int]
) -> dict[str, Any]:
    states = []
    for spec, state, count in zip(
        request["conversations"], batch["conversations"], counts
    ):
        prefix = state["items"][:count]
        trimmed = count < len(state["items"])
        states.append({
            **state,
            "items": prefix,
            "next_cursor": (
                _cursor_for_prefix(spec, state, prefix)
                if trimmed else state["next_cursor"]
            ),
            "has_more": state["has_more"] or trimmed,
        })
    return {
        **batch,
        "conversations": states,
        "counts": {"conversations": len(states), "items": sum(counts)},
        "status": (
            "partial" if any(state["has_more"] or state["gap_detected"]
                             for state in states) else "complete"
        ),
    }


def _within_batch_size_limits(
    batch: dict[str, Any], *, evidence: dict[str, Any], account_epoch: Any
) -> bool:
    """Match both downstream JSON sizes before any result leaves the worker."""
    public = {
        **batch,
        "multi_database_atomic": False,
        "source_consistency": "observed_quiet_window",
        "snapshot_scope": "one_authenticated_store_set",
    }
    worker = {
        **batch,
        "readstore_evidence": evidence,
        "readstore_account_epoch": account_epoch,
    }
    try:
        public_bytes = len(json.dumps(
            public, ensure_ascii=True, allow_nan=False
        ).encode("utf-8"))
        worker_bytes = len(json.dumps(
            worker, ensure_ascii=True, allow_nan=False,
            separators=(",", ":")
        ).encode("utf-8"))
    except (TypeError, ValueError, RecursionError):
        raise ProviderError("readstore_result_invalid") from None
    return (public_bytes <= MAX_BATCH_PUBLIC_BYTES
            and worker_bytes <= MAX_READSTORE_RESULT_BYTES)


def _pack_batch_page(
    batch: dict[str, Any], request: dict[str, Any], *,
    evidence: dict[str, Any], account_epoch: Any, deadline: float
) -> dict[str, Any]:
    if _within_batch_size_limits(batch, evidence=evidence, account_epoch=account_epoch):
        return batch
    counts = [0] * len(batch["conversations"])
    page = _prefix_page(batch, request, counts)
    if not _within_batch_size_limits(page, evidence=evidence, account_epoch=account_epoch):
        raise ProviderError("readstore_size_limit")
    # An unreturnable first row in any room would make its cursor stall on
    # every continuation.  Withhold the entire batch with a fixed size code.
    for index, state in enumerate(batch["conversations"]):
        if not state["items"]:
            continue
        proposed = list(counts)
        proposed[index] = 1
        if not _within_batch_size_limits(
            _prefix_page(batch, request, proposed),
            evidence=evidence, account_epoch=account_epoch,
        ):
            raise ProviderError("readstore_size_limit")
    while True:
        _check_deadline(deadline)
        advanced = False
        # One row per room per round protects later rooms from a full early room.
        for index, state in enumerate(batch["conversations"]):
            if counts[index] >= len(state["items"]):
                continue
            proposed = list(counts)
            proposed[index] += 1
            candidate = _prefix_page(batch, request, proposed)
            if _within_batch_size_limits(
                candidate, evidence=evidence, account_epoch=account_epoch
            ):
                counts, page = proposed, candidate
                advanced = True
        if not advanced:
            break
    # Every room with pending rows must make progress in a returned page.
    # Otherwise a busy earlier room can take the only fitting slot on every
    # continuation, leaving a later room at the same cursor indefinitely.
    if any(count == 0 and state["items"]
           for count, state in zip(counts, batch["conversations"])):
        raise ProviderError("readstore_size_limit")
    if not valid_batch_read_result(page, request):
        raise ProviderError("readstore_result_invalid")
    return page


def run_batch_read(
    args: Any,
    *,
    target: Any,
    deadline: int | float,
    open_stores: Callable[..., Any],
) -> dict[str, Any]:
    """Read all requested rooms under one authenticated open-store context.

    The returned cursors describe bounded rowid continuation. They are not a
    claim of atomic cross-database state, complete history, or exact-once
    delivery. No result escapes until the shared store context has revalidated
    and closed successfully.
    """

    request = _validated_request(args)
    _check_deadline(deadline)

    with open_stores(target, deadline=deadline, include_messages=True) as opened:
        account_epoch = opened.get("account_epoch")
        if account_epoch != request["account_epoch"]:
            raise ProviderError("readstore_account_changed")
        _check_deadline(deadline)

        connections = opened["connections"]
        message_shard_ids = sorted(opened["message_shard_ids"])
        message_connections = {
            shard_id: connections[shard_id] for shard_id in message_shard_ids
        }
        query = ReadStoreQuery(
            session_connection=connections["session"],
            message_connections=message_connections,
            account_epoch=account_epoch,
            query_timeout_seconds=5,
        )
        callback_errors: list[ProviderError] = []

        def read_one(expected_epoch: Any, spec: dict[str, Any]) -> dict[str, Any]:
            try:
                _check_deadline(deadline)
                if expected_epoch != account_epoch:
                    raise ProviderError("readstore_account_changed")
                conversation_key = spec["conversation_key"]
                chat_md5 = resolve_conversation(
                    connections["session"], conversation_key, deadline=deadline
                )
                raw = query.get_new_messages(
                    chat_md5,
                    limit=spec["limit"],
                    cursor=spec["cursor"],
                    start_from=(
                        spec["start_from"] if spec["cursor"] is None else "beginning"
                    ),
                    deadline=deadline,
                )
                from .readstore_sender_roles import classify_message_rows
                raw["items"] = classify_message_rows(
                    raw.get("items", []),
                    message_connections=message_connections,
                    account_username=opened.get("account_username"),
                    conversation_key=conversation_key,
                    contact_connection=opened.get("connections", {}).get("contact"),
                )
                return _single_query_result(
                    raw,
                    conversation_key=conversation_key,
                    chat_md5=chat_md5,
                    account_epoch=account_epoch,
                    shard_ids=message_shard_ids,
                    limit=spec["limit"],
                )
            except ProviderError as exc:
                callback_errors.append(exc)
                raise
            except (IdentityError, ReadStoreError) as exc:
                # Preserve only fixed budget and output-size categories.
                # Other provider details retain the generic boundary.
                if exc.code == "query_budget_exceeded":
                    code = "query_budget_exceeded"
                elif isinstance(exc, ReadStoreError) and exc.code in (
                    "row_too_large", "output_budget_exceeded"
                ):
                    code = "readstore_size_limit"
                else:
                    code = "readstore_unavailable"
                mapped = ProviderError(code)
                callback_errors.append(mapped)
                raise mapped from None

        try:
            batch = BatchReadProvider(read_one=read_one).read(
                account_epoch=account_epoch,
                conversations=request["conversations"],
                max_total=request["max_total"],
            )
        except BatchReadProviderError as exc:
            if callback_errors:
                raise callback_errors[0] from None
            code = {
                "batch_read_account_changed": "readstore_account_changed",
                "batch_read_result_invalid": "readstore_result_invalid",
            }.get(exc.code, "readstore_unavailable")
            raise ProviderError(code) from None
        _check_deadline(deadline)
        evidence = opened["evidence"]
        batch = _pack_batch_page(
            batch, request, evidence=evidence,
            account_epoch=account_epoch, deadline=deadline,
        )

    return {
        **batch,
        "readstore_evidence": evidence,
        "readstore_account_epoch": account_epoch,
    }


__all__ = ["run_batch_read"]
