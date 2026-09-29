# Native attachment bridge
## 简体中文摘要

本目录包含 Windows x64 文件附件桥接器的源码和构建脚本；对应 DLL 与指纹记录在 [`../src/wxbg/native/manifest.json`](../src/wxbg/native/manifest.json)。文件提交会通过 Windows 线程钩子把 DLL 加载到微信 UI 进程，首次启用后常驻至微信退出。它不是通用微信接口或远程执行接口。当前 v4 二进制虽有静态完整性及编译验证，但**没有实机微信提交验证**，本项目也未证明可从现有源码重建出逐字节相同的 DLL。查看下方 English build steps；不要公开本机编译报告或 `.zig-cache`。

This directory contains the source and build script for the version-pinned Windows x64 attachment bridge. The packaged DLL and its hash are recorded in [`../src/wxbg/native/manifest.json`](../src/wxbg/native/manifest.json). This component is not a general Weixin integration or a remote execution interface.

Attachment submission uses a Windows `SetWindowsHookExW` thread hook to load the bridge DLL into the Weixin UI process. It temporarily replaces one pinned import-address-table slot and restores that slot after the bounded operation. Once armed, the native DLL remains resident until Weixin exits; restoring the slot or stopping the MCP server does not unload it. The controller also uses a separate guarded `WriteProcessMemory` call on one pinned Weixin accessibility gate byte and attempts to restore that byte after the operation.

Builds require Python 3 and Zig 0.16.0. From the repository root:

```powershell
python .\native-src\build.py --verify-only
python .\native-src\build.py --compiler 'C:\path\to\zig.exe'
```

The script does not download a compiler, run the native test programs, or replace the packaged DLL. It verifies the packaged source and DLL hashes against the current manifest, but those source hashes do **not** match the historical binary build receipt. A bit-identical rebuild of the packaged DLL from this source has **not** been established. A successful compilation therefore does not authorize the new binary for live use: the controller verifies the pinned client DLL identity and packaged bridge hash. Compiler output may contain local paths; keep build reports and `.zig-cache` out of Git.

The controller requires the Weixin 4.1.13.12 `Weixin.dll` SHA-256 `e3240bf8a4d00593a4b3e6ce6c8b6ac26897622c27f410f6655c4eee17cb3b6d`. The bridge checks the loaded module's pinned x64 PE layout and code signature. Protocol v4 carries the attachment point and measured render-client width and height. Before queuing a click, the controller checks the semantic UIA attachment button and render mapping, and the bridge independently checks the minimized main window, one same-process render child, matching client dimensions and origin, and a bounded bottom-left point. No screen resolution or absolute point is pinned. The bridge has a bounded lease and rejects unsupported target layouts and files. The packaged bridge DLL has its own hash in the manifest. This rebuilt v4 DLL has no live Weixin submission verification; a locally created attachment card is not proof of upload completion or remote delivery. For current user-facing limits, see [`../CAPABILITIES.md`](../CAPABILITIES.md).
