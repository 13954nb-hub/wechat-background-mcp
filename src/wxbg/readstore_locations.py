"""Bounded internal account-directory selection; no keys or database content."""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import math
import ntpath
import os
from pathlib import Path
import re
import time


class LocationError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _identity(path):
    value = path.stat()
    return value.st_dev, value.st_ino


@dataclass(frozen=True)
class StorageLocation:
    path: Path = field(repr=False)
    account_path: Path = field(repr=False)
    storage_id: tuple = field(repr=False)
    account_id: tuple = field(repr=False)

    def revalidate(self):
        try:
            if (_identity(self.path) != self.storage_id
                    or _identity(self.account_path) != self.account_id
                    or self.path.resolve(strict=True) != self.path
                    or self.account_path.resolve(strict=True) != self.account_path):
                raise LocationError('storage_changed')
        except OSError:
            raise LocationError('storage_changed') from None


def parse_config_root(raw):
    """Accept a whole absolute drive path or an explicit known JSON field."""
    if type(raw) is not bytes or len(raw) > 8192:
        return None
    try:
        value = raw.decode('utf-8-sig').strip()
    except UnicodeDecodeError:
        return None
    if value.startswith('{'):
        try:
            obj = json.loads(value)
        except ValueError:
            return None
        if type(obj) is not dict:
            return None
        paths = [obj[k] for k in ('dataDir','data_dir','fileSavePath','savePath','path','defaultFileSavePath')
                 if k in obj and type(obj[k]) is str]
        if len({ntpath.normcase(ntpath.normpath(p.strip())) for p in paths}) != 1:
            return None
        value = paths[0].strip()
    if (not re.match(r'^[A-Za-z]:[\\/]', value)
            or len(value) > 1024 or any(ord(c) < 32 for c in value)):
        return None
    return Path(value)


def _registry_roots(check):
    import winreg
    result = []
    for subkey in (r'Software\Tencent\xwechat', r'Software\Tencent\xwechat\config',
                   r'Software\Tencent\WeChat'):
        check()
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, subkey, 0, winreg.KEY_READ) as key:
                count = winreg.QueryInfoKey(key)[1]
                if count > 128:
                    raise LocationError('storage_limit')
                for index in range(count):
                    check()
                    name, data, kind = winreg.EnumValue(key, index)
                    if type(data) is str and len(data) <= 1024 and any(s in name.lower() for s in ('path','dir','save')):
                        path = parse_config_root(data.encode('utf-8'))
                        if path is not None:
                            result.append(path)
        except FileNotFoundError:
            continue
        except OSError:
            raise LocationError('storage_unavailable') from None
    return result


def discover_roots(*, deadline, environment=None, registry_roots=None):
    """Read only bounded config metadata, known registry keys and defaults.

    Discovery does not select an account. All resulting candidates must pass
    select_storage using a freshly process-derived username.
    """
    if type(deadline) not in (int,float) or not math.isfinite(deadline):
        raise LocationError('storage_input_invalid')
    def check():
        if time.monotonic() >= deadline:
            raise LocationError('storage_deadline')
    check()
    environment = os.environ if environment is None else environment
    found, count = [], 0
    for variable in ('APPDATA','LOCALAPPDATA'):
        base = environment.get(variable)
        if not base:
            continue
        for suffix in ('Tencent/xwechat', 'Tencent/xwechat/config', 'Tencent/WeChat'):
            check()
            directory = Path(base)/suffix
            try:
                with os.scandir(directory) as iterator:
                    for entry in iterator:
                        check()
                        count += 1
                        if count > 128:
                            raise LocationError('storage_limit')
                        if not entry.is_file(follow_symlinks=False):
                            continue
                        with open(entry.path, 'rb') as stream:
                            if os.fstat(stream.fileno()).st_size > 8192:
                                continue
                            raw = stream.read(8193)
                        check()
                        path = parse_config_root(raw)
                        if path is not None:
                            found.append(path)
            except FileNotFoundError:
                continue
            except OSError:
                raise LocationError('storage_unavailable') from None
    supplied = _registry_roots(check) if registry_roots is None else registry_roots
    for index, path in enumerate(supplied):
        check()
        if index >= 32:
            raise LocationError('storage_limit')
        found.append(Path(path))
    profile = environment.get('USERPROFILE')
    if profile:
        found.extend((Path(profile)/'Documents', Path(profile)))
    result = list(dict.fromkeys(found))
    if len(result) > 64:
        raise LocationError('storage_limit')
    check()
    return result


def select_storage(username, *, roots, deadline, max_entries=256):
    """Select exactly one account matching the process-derived identity.

    No first-hit fallback. Directory aliases resolving to the same physical
    account are deduplicated; child junction/symlink escapes are rejected.
    """
    if (type(username) is not str or not username or len(username) > 256
            or any(c in username for c in '\\/:\0')
            or type(max_entries) is not int or not 0 < max_entries <= 256
            or type(deadline) not in (int,float) or not math.isfinite(deadline)):
        raise LocationError('storage_input_invalid')
    def check():
        if time.monotonic() >= deadline:
            raise LocationError('storage_deadline')
    visited, found, entries = set(), {}, 0
    for number, root in enumerate(roots):
        check()
        if number >= 64:
            raise LocationError('storage_limit')
        for suffix in ('', 'xwechat_files', 'WeChat Files', 'xwechat_files_data'):
            check()
            try:
                base = (Path(root)/suffix).resolve(strict=True)
                if base in visited or not base.is_dir():
                    continue
                visited.add(base)
                with os.scandir(base) as iterator:
                    for entry in iterator:
                        check()
                        entries += 1
                        if entries > max_entries:
                            raise LocationError('storage_limit')
                        name = entry.name
                        matched = name == username or (name.startswith(username+'_')
                            and re.fullmatch(r'[A-Za-z0-9_]{4}', name[len(username)+1:]))
                        if not matched or not entry.is_dir(follow_symlinks=False):
                            continue
                        account = Path(entry.path)
                        storage = account/'db_storage'
                        if not storage.is_dir():
                            continue
                        if account.resolve(strict=True) != account or storage.resolve(strict=True) != storage:
                            raise LocationError('storage_changed')
                        sid, aid = _identity(storage), _identity(account)
                        found[(aid,sid)] = StorageLocation(storage, account, sid, aid)
            except FileNotFoundError:
                continue
            except OSError:
                raise LocationError('storage_unavailable') from None
    check()
    if len(found) != 1:
        raise LocationError('storage_ambiguous' if found else 'storage_unavailable')
    location = next(iter(found.values()))
    location.revalidate()
    return location
