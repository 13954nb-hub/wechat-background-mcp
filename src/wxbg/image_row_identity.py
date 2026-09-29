"""Pure, bounded reconciliation for an existing non-image file-card row."""

import hashlib
import json
import re

from .file_card_contract import parse_file_card
from .policy import AdapterError


_KIND_RE = re.compile(r"mmui::Chat[A-Za-z0-9_]{0,80}ItemView\Z")
_REF_RE = re.compile(r"[0-9a-f]{32}\Z")
_ROW_KEYS = frozenset(("kind", "runtime", "ref", "name", "rectangle"))
_MAX_ROWS = 256
_MAX_RUNTIME_LENGTH = 256
_MAX_NAME_LENGTH = 4096
_MIN_COORDINATE = -32768
_MAX_COORDINATE = 32767
_FILE_SUFFIXES = (".txt", ".pdf", ".zip")
_TIME_NAME_RE = re.compile(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]\Z")


def _invalid():
    raise AdapterError("image_identity_invalid")


def _digest(value):
    encoded = json.dumps(value, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("ascii")).hexdigest()


def _name_hash(name):
    return hashlib.sha256(name.encode("utf-8")).hexdigest()


def _validate_fixture_name(value):
    if type(value) is not str or not value or len(value) > _MAX_NAME_LENGTH or "\x00" in value:
        _invalid()
    return value


def _validate_rows(rows):
    if type(rows) not in (list, tuple) or len(rows) > _MAX_ROWS:
        _invalid()
    validated = []
    runtimes = set()
    refs = set()
    for row in rows:
        if type(row) is not dict or set(row) != _ROW_KEYS:
            _invalid()
        kind = row["kind"]
        runtime = row["runtime"]
        reference = row["ref"]
        name = row["name"]
        rectangle = row["rectangle"]
        if (type(kind) is not str or _KIND_RE.fullmatch(kind) is None
                or type(runtime) is not str or not runtime
                or len(runtime) > _MAX_RUNTIME_LENGTH
                or type(reference) is not str or _REF_RE.fullmatch(reference) is None
                or type(name) is not str or len(name) > _MAX_NAME_LENGTH
                or "\x00" in name
                or type(rectangle) not in (list, tuple) or len(rectangle) != 4
                or any(type(value) is not int
                       or not _MIN_COORDINATE <= value <= _MAX_COORDINATE
                       for value in rectangle)):
            _invalid()
        if runtime in runtimes or reference in refs:
            _invalid()
        runtimes.add(runtime)
        refs.add(reference)
        validated.append({
            "kind": kind,
            "runtime": runtime,
            "ref": reference,
            "name": name,
            "rectangle": tuple(rectangle),
        })
    return tuple(validated)


def _semantic(row):
    return (row["kind"], _name_hash(row["name"]), len(row["name"]), row["rectangle"])


def _stable_semantic(row):
    return (row["kind"], _name_hash(row["name"]), len(row["name"]))


def _runtime_hash(rows):
    return _digest([row["runtime"] for row in rows])


def _semantic_hash(rows):
    return _digest([_semantic(row) for row in rows])


class Reconciler:
    """Reconcile only a uniquely identifiable existing non-image file card."""

    def __init__(self, baseline_rows, fixture_name):
        self._fixture_name = _validate_fixture_name(fixture_name)
        self._baseline = _validate_rows(baseline_rows)
        self._baseline_semantics = tuple(_semantic(row) for row in self._baseline)
        self._baseline_stable = tuple(_stable_semantic(row) for row in self._baseline)
        self._semantic_counts = {
            semantic: self._baseline_semantics.count(semantic)
            for semantic in set(self._baseline_semantics)
        }
        self._stable_counts = {
            semantic: self._baseline_stable.count(semantic)
            for semantic in set(self._baseline_stable)
        }
        self._runtime_owner = {
            row["runtime"]: index for index, row in enumerate(self._baseline)
        }
        self._ref_owner = {
            row["ref"]: index for index, row in enumerate(self._baseline)
        }
        self._baseline_runtime_ids = frozenset(self._runtime_owner)
        self._accepted_aliases = set()
        self._ignored = set(self._baseline_runtime_ids)
        self._alias_cap = len(self._baseline) * 5

    @property
    def ignored_runtime_ids(self):
        return frozenset(self._ignored)

    @property
    def ignored_refs(self):
        return frozenset(self._ref_owner)

    @property
    def baseline_summary(self):
        return {
            "row_count": len(self._baseline),
            "runtime_sha256": _runtime_hash(self._baseline),
            "semantic_sha256": _semantic_hash(self._baseline),
        }

    def _validate_known_aliases(self, rows, *, allow_separator_updates):
        owners = []
        for row in rows:
            runtime_owner = self._runtime_owner.get(row["runtime"])
            ref_owner = self._ref_owner.get(row["ref"])
            if (runtime_owner is not None and ref_owner is not None
                    and runtime_owner != ref_owner):
                _invalid()
            owner = runtime_owner if runtime_owner is not None else ref_owner
            owners.append(owner)
            if owner is not None:
                separator_update = (
                    allow_separator_updates
                    and runtime_owner is not None
                    # Adapter.ref includes Name for non-session rows. A clock
                    # label update therefore changes its ref while retaining
                    # the known runtime; it remains excluded from new images.
                    and (ref_owner is None or runtime_owner == ref_owner)
                    and self._baseline[owner]["kind"] == "mmui::ChatItemView"
                    and row["kind"] == "mmui::ChatItemView"
                    and _TIME_NAME_RE.fullmatch(self._baseline[owner]["name"]) is not None
                    and _TIME_NAME_RE.fullmatch(row["name"]) is not None
                )
                if (_stable_semantic(row) != self._baseline_stable[owner]
                        and not separator_update):
                    _invalid()
        return tuple(owners)

    def _eligible_rebind(self, row, index):
        semantic = _semantic(row)
        stable = _stable_semantic(row)
        if (row["kind"] != "mmui::ChatBubbleItemView"
                or self._semantic_counts.get(semantic) != 1
                or self._stable_counts.get(stable) != 1):
            return False
        try:
            parsed = parse_file_card(row["name"])
        except Exception:
            return False
        filename = parsed.get("filename") if type(parsed) is dict else None
        if type(filename) is not str:
            return False
        folded = filename.casefold()
        if folded == self._fixture_name.casefold():
            return False
        if not any(folded.endswith(suffix) for suffix in _FILE_SUFFIXES):
            return False
        return _stable_semantic(row) == self._baseline_stable[index]

    def _eligible_scene_rebind(self, row):
        if (row['kind'] not in ('mmui::ChatItemView','mmui::ChatTextItemView',
                                'mmui::ChatBubbleItemView','mmui::ChatBubbleReferItemView')
                or self._semantic_counts.get(_semantic(row)) != 1):
            return False
        if row['kind'] == 'mmui::ChatItemView':
            return _TIME_NAME_RE.fullmatch(row['name']) is not None
        try:
            parsed = parse_file_card(row['name'])
        except Exception:
            parsed = None
        return not (isinstance(parsed,dict) and type(parsed.get('filename')) is str
                    and parsed['filename'].casefold() == self._fixture_name.casefold())

    def reconcile(self, current_rows, *, allow_separator_updates=False, allow_scene_rebind=False):
        if (type(allow_separator_updates) is not bool or type(allow_scene_rebind) is not bool
                or allow_separator_updates and allow_scene_rebind):
            _invalid()
        current = _validate_rows(current_rows)
        owners = self._validate_known_aliases(
            current, allow_separator_updates=allow_separator_updates
        )
        rebound_count = 0
        same_semantics = (
            len(current) == len(self._baseline)
            and tuple(_semantic(row) for row in current) == self._baseline_semantics
        )
        candidates = []
        if same_semantics:
            for index, row in enumerate(current):
                baseline = self._baseline[index]
                changed = (row["runtime"], row["ref"]) != (
                    baseline["runtime"], baseline["ref"]
                )
                if not changed:
                    continue
                owner = owners[index]
                if owner is not None and owner != index:
                    _invalid()
                if (row["runtime"] not in self._runtime_owner
                        or row["ref"] not in self._ref_owner):
                    candidates.append((index, row))

            if candidates and not all(
                    (self._eligible_scene_rebind(row) if allow_scene_rebind else self._eligible_rebind(row, index))
                    for index, row in candidates):
                candidates = []
            new_runtime_aliases = [
                row for _, row in candidates
                if row["runtime"] not in self._runtime_owner
            ]
            if len(self._ignored) + len(new_runtime_aliases) > self._alias_cap:
                _invalid()
            rebound_count = len(new_runtime_aliases)
            for index, row in candidates:
                if row["runtime"] not in self._runtime_owner:
                    self._runtime_owner[row["runtime"]] = index
                    self._accepted_aliases.add(row["runtime"])
                    self._ignored.add(row["runtime"])
                if row["ref"] not in self._ref_owner:
                    self._ref_owner[row["ref"]] = index

        return {
            "row_count": len(current),
            "runtime_sha256": _runtime_hash(current),
            "semantic_sha256": _semantic_hash(current),
            "rebound_count": rebound_count,
            "ignored_rebound_total": len(self._accepted_aliases),
        }
