"""Verify and compile the pinned native sources; never execute a built artifact."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys


HERE = Path(__file__).resolve().parent
NATIVE = HERE.parent / "src" / "wxbg" / "native"
PINNED_SHA256 = "da922434618ae61efbd7eeaf43f277e1cded870d5a3b3e153629b29e75413594"
CORE = ["attachment_bridge.cpp", "file_dialog_proxy.cpp", "iat_lease.cpp", "fixture_grant.cpp"]
FLAGS = ["-target", "x86_64-windows-gnu", "-std=c++17", "-O2", "-Wall", "-Wextra", "-Werror", "-DUNICODE", "-D_UNICODE"]
LIBRARIES = ["-luser32", "-lkernel32", "-lole32", "-lshell32", "-luuid", "-lpsapi", "-lshlwapi", "-lbcrypt"]


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_package() -> dict:
    manifest = json.loads((NATIVE / "manifest.json").read_text(encoding="utf-8"))
    if manifest["protocol_version"] != 4 or manifest["request_struct_size"] != 736:
        raise ValueError("Unexpected protocol or request structure size")
    binary = manifest["binary"]
    if binary["sha256"] != PINNED_SHA256 or binary["path"] != f"{PINNED_SHA256}/wxbg_attachment.dll":
        raise ValueError("Manifest does not identify the pinned binary")
    entries = [(NATIVE / binary["path"], binary)]
    for name, details in manifest["files"].items():
        if Path(name).name != name:
            raise ValueError("Source manifest paths must be basenames")
        entries.append((HERE / name, details))
    for path, details in entries:
        if path.stat().st_size != details["size_bytes"] or sha256(path) != details["sha256"]:
            raise ValueError(f"Integrity check failed: {path.name}")
    return {"verified": True, "binary_sha256": PINNED_SHA256, "source_bundle_files": len(entries) - 1}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compiler", type=Path, help="Path to Zig 0.16.0 executable")
    parser.add_argument("--verify-only", action="store_true", help="Hash checks only; no compiler execution")
    parser.add_argument("--with-tests", action="store_true", help="Compile owned fixtures and test executables; never run them")
    parser.add_argument("--out-dir", type=Path, default=HERE / "build", help="New/empty output directory")
    parser.add_argument("--cache-dir", type=Path, default=HERE / ".zig-cache", help="Zig cache directory")
    args = parser.parse_args()
    integrity = verify_package()
    print(json.dumps(integrity), flush=True)
    if args.verify_only:
        if args.with_tests:
            parser.error("--with-tests cannot be combined with --verify-only")
        return 0
    if args.compiler is None:
        parser.error("--compiler is required to build; use --verify-only for hash checks")
    compiler = args.compiler.resolve(strict=True)
    out = args.out_dir.resolve()
    cache = args.cache_dir.resolve()
    if out == NATIVE or NATIVE in out.parents or out == HERE or out in HERE.parents:
        raise ValueError("Build output must be separate from the source and packaged native files")
    if out.exists() and any(out.iterdir()):
        raise ValueError("Output directory must be empty; existing artifacts are never overwritten")
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    version = subprocess.run([str(compiler), "version"], check=True, capture_output=True, text=True,
                             creationflags=creationflags).stdout.strip()
    if version != "0.16.0":
        raise ValueError(f"Expected Zig 0.16.0, received {version!r}")
    out.mkdir(parents=True, exist_ok=True)
    cache.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, ZIG_GLOBAL_CACHE_DIR=str(cache / "global"), ZIG_LOCAL_CACHE_DIR=str(cache / "local"))
    targets = [("wxbg_attachment.dll", CORE, ["-shared"])]
    if args.with_tests:
        targets += [
            ("attachment_fixture.dll", ["attachment_fixture.cpp"], ["-shared"]),
            ("iat_fixture.dll", ["iat_fixture.cpp"], ["-shared"]),
            ("attachment_bridge_test.exe", ["attachment_bridge_test.cpp", *CORE[1:]], []),
            ("file_dialog_proxy_test.exe", ["file_dialog_proxy.cpp", "file_dialog_proxy_test.cpp"], []),
            ("iat_lease_test.exe", ["iat_lease.cpp", "iat_lease_test.cpp"], ["-municode"]),
            ("iat_lease_fault_test.exe", ["iat_lease.cpp", "iat_lease_test.cpp"], ["-municode", "-DWXBG_IAT_TESTING"]),
            ("fixture_grant_test.exe", ["fixture_grant.cpp", "fixture_grant_test.cpp"], []),
        ]
    records = []
    for name, sources, extra in targets:
        # Production option order matches the historical receipt.
        flags = FLAGS[:3] + extra + FLAGS[3:]
        command = [str(compiler), "c++", *flags, *[str(HERE / source) for source in sources],
                   "-o", str(out / name), *LIBRARIES]
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                env=env, creationflags=creationflags)
        diagnostics = result.stdout + result.stderr
        log_name = name + ".build.log"
        (out / log_name).write_text(diagnostics, encoding="utf-8")
        if result.returncode:
            print(diagnostics[-6000:], file=sys.stderr)
            raise RuntimeError(f"Compilation failed for {name}: exit {result.returncode}")
        digest = sha256(out / name)
        record = {"output": name, "sha256": digest, "size_bytes": (out / name).stat().st_size,
                  "arguments": ["c++", *flags, *sources, "-o", name, *LIBRARIES], "executed": False,
                  "diagnostics_log": log_name, "diagnostics_characters": len(diagnostics)}
        if name == "wxbg_attachment.dll":
            record["matches_packaged_binary"] = digest == PINNED_SHA256
        records.append(record)
        print(json.dumps(record), flush=True)
    # A successful rebuild must still correspond to the verified source bytes.
    verify_package()
    report = {"compiler": "Zig", "compiler_version": version, "target": "x86_64-windows-gnu",
              "integrity": integrity, "artifacts": records, "artifacts_executed": False,
              "bit_identical_required": False,
              "note": "Compiler debug paths may differ after relocation. Only the separately pinned DLL is authorized by this package manifest."}
    (out / "build-report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1)
