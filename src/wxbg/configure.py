"""Read-only setup helper that prints an MCP client configuration.

It never edits Codex settings, changes Weixin, starts the MCP server, or
selects an account.  Runtime validation still verifies the loaded module.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
import sys

from . import __version__
from .gate import EXPECTED_DLL_SIZE, EXPECTED_SHA, GateError, WinBackend
from .path_config import ClientPathError, DEFAULT_EXE, load_client_paths, local_absolute_path


class SetupError(ValueError):
    pass


def _running_executables() -> list[str]:
    """Inspect process metadata only; never scan disks or attach to a client."""
    try:
        import psutil
    except ImportError as exc:
        raise SetupError("psutil is required to inspect running Weixin processes") from exc
    found: dict[str, str] = {}
    for process in psutil.process_iter(attrs=("name", "exe")):
        try:
            info = process.info
            if str(info.get("name") or "").casefold() != "weixin.exe":
                continue
            raw = info.get("exe")
            if not raw:
                continue
            path = local_absolute_path(raw, filename="Weixin.exe")
            found[os.path.normcase(os.path.realpath(path))] = str(path)
        except (psutil.NoSuchProcess, psutil.AccessDenied, ClientPathError, OSError):
            continue
        if len(found) > 16:
            raise SetupError("too many Weixin process paths; pass --weixin-exe")
    return sorted(found.values(), key=str.casefold)


def _choose_paths(args: argparse.Namespace):
    environment = dict(os.environ)
    if args.weixin_exe is not None:
        environment["WXBG_WEIXIN_EXE"] = args.weixin_exe
        if args.weixin_dll is None:
            environment.pop("WXBG_WEIXIN_DLL", None)
    elif "WXBG_WEIXIN_EXE" not in environment:
        running = _running_executables()
        if len(running) > 1:
            raise SetupError("multiple Weixin installations are running; pass --weixin-exe")
        if running:
            environment["WXBG_WEIXIN_EXE"] = running[0]
            if args.weixin_dll is None:
                environment.pop("WXBG_WEIXIN_DLL", None)
        elif not DEFAULT_EXE.is_file():
            raise SetupError("no running Weixin installation found; pass --weixin-exe")
    if args.weixin_dll is not None:
        environment["WXBG_WEIXIN_DLL"] = args.weixin_dll
    return load_client_paths(environment)


def _verify_client(exe: Path, dll: Path) -> None:
    if not exe.is_file():
        raise SetupError("Weixin.exe is unavailable at the configured path")
    try:
        with dll.open("rb") as stream:
            if os.fstat(stream.fileno()).st_size != EXPECTED_DLL_SIZE:
                raise SetupError("Weixin.dll size does not match the supported 4.1.13.12 build")
            header = stream.read(4096)
            stream.seek(0)
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
    except OSError as exc:
        raise SetupError("Weixin.dll is unavailable at the configured path") from exc
    if digest != EXPECTED_SHA:
        raise SetupError("Weixin.dll SHA-256 does not match the supported 4.1.13.12 build")
    try:
        WinBackend._check_pe(header)
    except GateError as exc:
        raise SetupError("Weixin.dll PE layout does not match the supported build") from exc


def _state_dir(raw: str | None) -> Path:
    if raw is None:
        raw = os.environ.get("WXBG_STATE_DIR")
    if raw is None:
        base = os.environ.get("LOCALAPPDATA")
        if not base:
            raise SetupError("LOCALAPPDATA is unavailable; pass --state-dir")
        raw = str(Path(base) / "WeChatBackgroundMCP")
    return local_absolute_path(raw)


def _runtime_path(raw: str, filename: str) -> Path:
    path = local_absolute_path(raw, filename=filename)
    if not path.is_file():
        raise SetupError(filename + " is unavailable at the configured path")
    return path


def _verify_python(python: Path, *, source_launch: bool) -> None:
    """Check the exact interpreter that the emitted MCP entry will launch."""
    probe = (
        "import importlib.metadata as m, json, struct, sys; "
        "name='wechat-background-mcp'; "
        "version=next((d.version for d in m.distributions() "
        "if d.metadata.get('Name', '').casefold().replace('_', '-')==name), None); "
        "print(json.dumps({'python':list(sys.version_info[:2]), "
        "'implementation':sys.implementation.name, "
        "'bits':struct.calcsize('P')*8, 'package':version}))"
    )
    try:
        result = subprocess.run(
            [str(python), "-I", "-c", probe],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=10, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SetupError("configured Python could not run its environment check") from exc
    if result.returncode != 0 or len(result.stdout) > 4096:
        raise SetupError("configured Python failed its environment check")
    try:
        details = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError) as exc:
        raise SetupError("configured Python returned an invalid environment check") from exc
    if (details.get("python") != [3, 12]
            or details.get("implementation") != "cpython"
            or details.get("bits") != 64):
        raise SetupError("configured Python must be CPython 3.12 x64")
    if not source_launch and details.get("package") != __version__:
        raise SetupError(
            "configured Python must have wechat-background-mcp " + __version__ + " installed"
        )


def _toml_string(value: str) -> str:
    # JSON string escapes are valid for these ordinary Windows paths in TOML.
    return json.dumps(value, ensure_ascii=False)


def _render_codex(command: str, arguments: list[str], environment: dict[str, str]) -> str:
    lines = [
        "[mcp_servers.WeChat_MCP]",
        "command = " + _toml_string(command),
        "args = [" + ", ".join(_toml_string(arg) for arg in arguments) + "]",
        "startup_timeout_sec = 30",
        "",
        "[mcp_servers.WeChat_MCP.env]",
    ]
    lines.extend(name + " = " + _toml_string(value) for name, value in environment.items())
    return "\n".join(lines)


def _render_json(command: str, arguments: list[str], environment: dict[str, str]) -> str:
    return json.dumps({"mcpServers": {"WeChat_MCP": {
        "command": command, "args": arguments, "env": environment,
    }}}, ensure_ascii=False, indent=2)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Print a verified, local WeChat MCP configuration")
    parser.add_argument("--weixin-exe", metavar="ABSOLUTE_PATH",
                        help="path to the supported local Weixin.exe")
    parser.add_argument("--weixin-dll", metavar="ABSOLUTE_PATH",
                        help="optional Weixin.dll path; otherwise derived from the executable")
    parser.add_argument("--state-dir", metavar="ABSOLUTE_PATH",
                        help="persistent directory shared by every MCP client for this Windows user")
    parser.add_argument("--data-root", metavar="ABSOLUTE_PATH",
                        help="optional Weixin data parent; runtime still verifies the logged-in account")
    parser.add_argument("--python", metavar="ABSOLUTE_PATH",
                        help="CPython 3.12 x64 interpreter with this package version installed")
    parser.add_argument("--launch", metavar="ABSOLUTE_PATH",
                        help="optional source checkout launch.py; installed packages use -m wxbg")
    parser.add_argument("--format", choices=("codex", "json"), default="codex")
    parser.add_argument("--list-running", action="store_true",
                        help="list currently running Weixin executable paths and exit")
    args = parser.parse_args(argv)
    try:
        if os.name != "nt" or struct.calcsize("P") != 8:
            raise SetupError("a 64-bit Windows Python process is required")
        if args.list_running:
            print(json.dumps({"weixin_executables": _running_executables()}, ensure_ascii=False, indent=2))
            return 0
        paths = _choose_paths(args)
        _verify_client(paths.exe, paths.dll)
        state = _state_dir(args.state_dir)
        python = _runtime_path(args.python or sys.executable, "python.exe")
        _verify_python(python, source_launch=args.launch is not None)
        arguments = ([str(_runtime_path(args.launch, "launch.py"))]
                     if args.launch is not None else ["-m", "wxbg"])
        environment = {
            "PYTHONIOENCODING": "utf-8",
            "WXBG_WEIXIN_EXE": str(paths.exe),
            "WXBG_WEIXIN_DLL": str(paths.dll),
            "WXBG_STATE_DIR": str(state),
        }
        if args.data_root is not None:
            data_root = local_absolute_path(args.data_root)
            if not data_root.is_dir():
                raise SetupError("Weixin data root is unavailable at the configured path")
            environment["WXBG_DATA_ROOT"] = str(data_root)
        print((_render_json if args.format == "json" else _render_codex)(
            str(python), arguments, environment))
        return 0
    except (ClientPathError, SetupError) as exc:
        print("configuration error: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
