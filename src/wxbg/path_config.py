"""Install-time client paths; binary identity remains pinned elsewhere.

Only the location of the supported Weixin build is configurable.  A path
override never changes the approved hash, PE layout, offsets, or UI layout.
The same environment is inherited by the gateway, guardian, and worker.
"""
from __future__ import annotations

from dataclasses import dataclass
import ntpath
import os
from pathlib import Path
import re
from typing import Mapping


CLIENT_BUILD = "4.1.13.12"
DEFAULT_EXE = Path(r"C:\Weixin\Weixin.exe")
DEFAULT_DLL = Path(r"C:\Weixin\4.1.13.12\Weixin.dll")


class ClientPathError(ValueError):
    """A path cannot safely identify the pinned local Weixin installation."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ClientPaths:
    exe: Path
    dll: Path


def local_absolute_path(raw: str, *, filename: str | None = None) -> Path:
    """Accept one ordinary absolute drive path, never a share or device path."""
    if (type(raw) is not str or not raw or len(raw) > 32767
            or any(ord(character) < 32 for character in raw)
            or any(character in raw for character in '<>"|*?')
            or raw.startswith(("\\\\", "//"))):
        raise ClientPathError("client_path_invalid")
    drive, tail = ntpath.splitdrive(raw)
    if (re.fullmatch(r"[A-Za-z]:", drive) is None
            or not tail.startswith(("\\", "/")) or ":" in tail):
        raise ClientPathError("client_path_invalid")
    if filename is not None and ntpath.basename(raw).casefold() != filename.casefold():
        raise ClientPathError("client_path_invalid")
    try:
        return Path(raw).resolve(strict=False)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ClientPathError("client_path_invalid") from exc


def load_client_paths(environment: Mapping[str, str] | None = None) -> ClientPaths:
    """Resolve explicit paths or the legacy location, without filesystem scans.

    With a custom executable and no DLL override, the DLL is derived from the
    same installation directory and the pinned client build.  An invalid
    explicit setting fails closed rather than falling back to another client.
    """
    environment = os.environ if environment is None else environment
    raw_exe = environment.get("WXBG_WEIXIN_EXE")
    raw_dll = environment.get("WXBG_WEIXIN_DLL")
    if raw_exe is None:
        exe = DEFAULT_EXE
    else:
        exe = local_absolute_path(raw_exe, filename="Weixin.exe")
    if raw_dll is not None:
        dll = local_absolute_path(raw_dll, filename="Weixin.dll")
    elif raw_exe is not None:
        dll = exe.parent / CLIENT_BUILD / "Weixin.dll"
    else:
        dll = DEFAULT_DLL
    return ClientPaths(exe=exe, dll=dll)
