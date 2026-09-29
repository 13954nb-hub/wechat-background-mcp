"""Closed, private contract for image-entry observation evidence."""

import re


_TOP_KEYS = frozenset(
    ("version", "baseline", "selection_frames", "final_frames")
)
_BASELINE_KEYS = frozenset(
    ("row_count", "runtime_sha256", "semantic_sha256")
)
_FRAME_KEYS = frozenset(
    (
        "index",
        "draft_kind",
        "row_count",
        "runtime_sha256",
        "semantic_sha256",
        "rebound_count",
        "ignored_rebound_total",
        "new_rows_count",
        "rows_seen",
        "image_novelty",
        "embedded_observations",
        "stable_count",
    )
)
_SELECTION_DIAGNOSTIC_KEYS = frozenset((
    "draft_initial_kind", "draft_first_recheck_kind",
    "scene_rebind_enabled", "same_scene_content", "same_scene_geometry",
    "same_scene_runtime", "new_row_kind_counts", "new_row_label_counts",
    "decision",
))
_ROW_KIND_COUNT_KEYS = frozenset(("text", "bubble", "bubble_refer", "separator", "other"))
_ROW_LABEL_COUNT_KEYS = frozenset(("image_label", "owned_file_card", "other"))
_SELECTION_DECISIONS = frozenset((
    "continue", "automatic_transition", "presend", "sticky_rows_seen",
    "max_frames_exhausted", "draft_conflict",
))
_DRAFT_KINDS = frozenset(("empty", "embedded", "other"))
_LOWER_HEX64 = re.compile(r"[0-9a-f]{64}\Z")


def _invalid_value():
    return {
        "version": 1,
        "baseline": None,
        "selection_frames": [],
        "final_frames": [],
    }


def _bounded_int(value, minimum, maximum):
    return type(value) is int and minimum <= value <= maximum


def _hash64(value):
    if type(value) is not str:
        return False
    return _LOWER_HEX64.fullmatch(value) is not None


def _normalize_baseline(value):
    if value is None:
        return None, True
    if not isinstance(value, dict) or set(value) != _BASELINE_KEYS:
        return None, False
    if (not _bounded_int(value["row_count"], 0, 256)
            or not _hash64(value["runtime_sha256"])
            or not _hash64(value["semantic_sha256"])):
        return None, False
    return {
        "row_count": value["row_count"],
        "runtime_sha256": value["runtime_sha256"],
        "semantic_sha256": value["semantic_sha256"],
    }, True


def _normalize_counts(value, keys, expected_total):
    if not isinstance(value, dict) or set(value) != keys:
        return None, False
    if (any(not _bounded_int(item, 0, 8) for item in value.values())
            or sum(value.values()) != expected_total):
        return None, False
    return {key: value[key] for key in sorted(keys)}, True


def _normalize_selection_diagnostics(value, new_rows_count):
    if not isinstance(value, dict) or set(value) != _SELECTION_DIAGNOSTIC_KEYS:
        return None, False
    if (type(value.get("draft_initial_kind")) is not str
            or value["draft_initial_kind"] not in _DRAFT_KINDS
            or type(value.get("draft_first_recheck_kind")) is not str
            or value["draft_first_recheck_kind"] not in _DRAFT_KINDS
            or any(type(value.get(key)) is not bool for key in (
                "scene_rebind_enabled", "same_scene_content",
                "same_scene_geometry", "same_scene_runtime"))
            or type(value.get("decision")) is not str
            or value["decision"] not in _SELECTION_DECISIONS):
        return None, False
    kinds, kinds_valid = _normalize_counts(
        value["new_row_kind_counts"], _ROW_KIND_COUNT_KEYS, new_rows_count)
    labels, labels_valid = _normalize_counts(
        value["new_row_label_counts"], _ROW_LABEL_COUNT_KEYS, new_rows_count)
    if not (kinds_valid and labels_valid):
        return None, False
    return {
        "draft_initial_kind": value["draft_initial_kind"],
        "draft_first_recheck_kind": value["draft_first_recheck_kind"],
        "scene_rebind_enabled": value["scene_rebind_enabled"],
        "same_scene_content": value["same_scene_content"],
        "same_scene_geometry": value["same_scene_geometry"],
        "same_scene_runtime": value["same_scene_runtime"],
        "new_row_kind_counts": kinds,
        "new_row_label_counts": labels,
        "decision": value["decision"],
    }, True


def _normalize_frame(value, expected_index, *, selection):
    allowed = (_FRAME_KEYS, _FRAME_KEYS | {"diagnostics"}) if selection else (_FRAME_KEYS,)
    if not isinstance(value, dict) or set(value) not in allowed:
        return None, False
    if (not _bounded_int(value["index"], 1, 4)
            or value["index"] != expected_index
            or type(value["draft_kind"]) is not str
            or value["draft_kind"] not in _DRAFT_KINDS
            or not _bounded_int(value["row_count"], 0, 256)
            or not _hash64(value["runtime_sha256"])
            or not _hash64(value["semantic_sha256"])
            or not _bounded_int(value["rebound_count"], 0, 256)
            or not _bounded_int(value["ignored_rebound_total"], 0, 1024)
            or not _bounded_int(value["new_rows_count"], 0, 8)
            or type(value["rows_seen"]) is not bool
            or type(value["image_novelty"]) is not bool
            or not _bounded_int(value["embedded_observations"], 0, 4)
            or not _bounded_int(value["stable_count"], 0, 4)):
        return None, False
    fixed = {
        "index": value["index"],
        "draft_kind": value["draft_kind"],
        "row_count": value["row_count"],
        "runtime_sha256": value["runtime_sha256"],
        "semantic_sha256": value["semantic_sha256"],
        "rebound_count": value["rebound_count"],
        "ignored_rebound_total": value["ignored_rebound_total"],
        "new_rows_count": value["new_rows_count"],
        "rows_seen": value["rows_seen"],
        "image_novelty": value["image_novelty"],
        "embedded_observations": value["embedded_observations"],
        "stable_count": value["stable_count"],
    }
    if selection and "diagnostics" in value:
        diagnostics, valid = _normalize_selection_diagnostics(
            value["diagnostics"], value["new_rows_count"])
        if not valid:
            return None, False
        fixed["diagnostics"] = diagnostics
    return fixed, True


def _normalize_frames(value, *, selection):
    if not isinstance(value, list) or len(value) > 4:
        return [], False
    fixed = []
    for expected_index, item in enumerate(value, 1):
        normalized, valid = _normalize_frame(item, expected_index, selection=selection)
        if not valid:
            return [], False
        fixed.append(normalized)
    return fixed, True


def normalize_image_evidence(value):
    """Return a rebuilt safe evidence object and whether the input is valid."""

    if not isinstance(value, dict) or set(value) != _TOP_KEYS:
        return _invalid_value(), False
    if type(value["version"]) is not int or value["version"] != 1:
        return _invalid_value(), False

    baseline, baseline_valid = _normalize_baseline(value["baseline"])
    selection_frames, selection_valid = _normalize_frames(
        value["selection_frames"], selection=True)
    final_frames, final_valid = _normalize_frames(
        value["final_frames"], selection=False)
    if not (baseline_valid and selection_valid and final_valid):
        return _invalid_value(), False
    if baseline is None and (selection_frames or final_frames):
        return _invalid_value(), False

    return {
        "version": 1,
        "baseline": baseline,
        "selection_frames": selection_frames,
        "final_frames": final_frames,
    }, True


__all__ = ["normalize_image_evidence"]
