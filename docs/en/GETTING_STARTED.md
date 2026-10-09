# Installation and first deployment

[简体中文](../zh-CN/快速开始.md) · [Interface and display guide](INTERFACE_AND_DISPLAY.md) · [Repository home](../../README.md)

Read the bilingual [Disclaimer](../../DISCLAIMER.md) before installing.
Use the bilingual [29-tool catalog and workflow guide](../TOOLS_AND_WORKFLOWS.md) after setup.

## Release and requirements

WeChat Background MCP 2.6.0 is a local, experimental MCP server. The single installable package is a Windows x64 wheel for CPython 3.12 x64:

- [Download the 2.6.0 wheel](https://github.com/13954nb-hub/wechat-background-mcp/releases/download/v2.6.0/wechat_background_mcp-2.6.0-cp312-cp312-win_amd64.whl)
- SHA-256: [release checksums](https://github.com/13954nb-hub/wechat-background-mcp/releases/tag/v2.6.0)

| Component | Supported value |
| --- | --- |
| Operating system | Windows 10 or 11, x64 |
| Python | CPython 3.12, x64, in a dedicated virtual environment |
| Weixin desktop client | Weixin for Windows 4.1.13.12, x64 |
| Client integrity gate | Installed Weixin.dll SHA-256 must equal **e3240bf8a4d00593a4b3e6ce6c8b6ac26897622c27f410f6655c4eee17cb3b6d** |
| MCP transport | Local stdio process in the same interactive Windows desktop session as Weixin |

A displayed version number is not enough. The setup helper checks the selected executable path, DLL size, SHA-256, PE layout, Python architecture, and installed package version. Any mismatch fails closed.

## Weixin client version and download

The MCP wheel does **not** include the proprietary Weixin installer. Get Weixin from Tencent:

- [Official Weixin for Windows download page](https://pc.weixin.qq.com/)
- [Tencent CDN: WeChatWin_4.1.13.exe](https://dldir1v6.qq.com/weixin/Universal/Windows/WeChatWin_4.1.13.exe)
- [Independent version-history record for 4.1.13.12](https://github.com/cscnk52/wechat-windows-versions/releases/tag/v4.1.13.12)

The Tencent CDN URL is a mutable family URL, not an immutable 4.1.13.12 archive. A public version-history record reports that it served installer version 4.1.13.12 on 2026-08-21 with installer SHA-256 **74570be9fa1dbabf11a901e00e24279e139ade7277da4c8852d60faa6403345e**; later records show the same URL serving other builds. The history page is third-party metadata, not an installer mirror. Download only from Tencent or a source Tencent authorizes, inspect the actual downloaded file, and let the MCP setup helper verify the installed Weixin.dll. The installer hash and the installed DLL hash are different checks.

If the installer now provides a newer build, do not patch the MCP fingerprint, suppress Weixin updates, or silently downgrade the client. Stop and use a client build that is officially available and explicitly tested with a future MCP release.

## Download and verify the MCP wheel

In PowerShell, download the wheel and check its SHA-256 before installation:

    $url = 'https://github.com/13954nb-hub/wechat-background-mcp/releases/download/v2.6.0/wechat_background_mcp-2.6.0-cp312-cp312-win_amd64.whl'
    Invoke-WebRequest -Uri $url -OutFile .\wechat_background_mcp-2.6.0-cp312-cp312-win_amd64.whl

    $wheel = Join-Path $PWD 'wechat_background_mcp-2.6.0-cp312-cp312-win_amd64.whl'
    $checksumUrl = 'https://github.com/13954nb-hub/wechat-background-mcp/releases/download/v2.6.0/SHA256SUMS.txt'
    $checksumText = (Invoke-WebRequest -Uri $checksumUrl).Content
    $checksumLine = @($checksumText -split '\r?\n' | Where-Object { $_ -match '^[0-9a-f]{64}  wechat_background_mcp-2\.6\.0-cp312-cp312-win_amd64\.whl$' })
    if ($checksumLine.Count -ne 1) { throw 'Missing or ambiguous wheel checksum' }
    $expected = ($checksumLine[0] -split '  ')[0]
    $actual = (Get-FileHash -LiteralPath $wheel -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actual -ne $expected) { throw "Wheel SHA-256 mismatch: $actual" }

Create a dedicated environment and install the verified package:

    py -3.12 -m venv "$env:LOCALAPPDATA\WeChatBackgroundMCP\venv"
    $python = "$env:LOCALAPPDATA\WeChatBackgroundMCP\venv\Scripts\python.exe"
    & $python -m pip install $wheel
    & $python -m pip check

Do not use another AI product's embedded Python. Keep this environment and MCP state directory stable across restarts, outside the source repository.

## Create the MCP client configuration

The configuration helper only prints a configuration; it does not edit Codex settings or modify Weixin. First inspect running Weixin executable paths, then pass the exact user-selected path:

    $configure = "$env:LOCALAPPDATA\WeChatBackgroundMCP\venv\Scripts\wechat-mcp-configure.exe"
    & $configure --list-running
    & $configure --weixin-exe 'C:\actual\path\Weixin.exe' --format codex

For other MCP clients, use --format json. The generated entry includes the local Python executable and machine-specific environment paths. Review the output, back up the selected client's existing configuration, and add or replace only this MCP entry. Preserve unrelated settings. The helper does not guess among multiple Weixin installations and does not write client configuration.

Restart the MCP client, then call wechat_capabilities followed by the read-only wechat_status. Check cleanup.restored=true in the status result. The status call may briefly lease and restore a version-pinned access-gate byte in the Weixin process; see [Security](../../SECURITY.md).

## First-use workflow

1. Read the wechat_first_deploy_and_tool_chains MCP prompt.
2. Verify wechat_capabilities and wechat_status.
3. For a group or chat, run a fresh wechat_search; treat database results as candidates, then verify the exact identity in the live Weixin UI. A database key is not a sendable UI session reference.
4. Read only the requested conversation and time range. Sender roles are self, other, or unknown; only verified other messages may be considered for a reply.
5. Before any send, recheck the exact target and full unsent content. A local send result is not proof of remote delivery.
6. For multiple chats, use wechat_multi_chat_reply_loop. wechat_watch_new_messages is bounded client-driven polling, not a persistent server listener. Each call waits 1–60 seconds, supports up to 16 selected conversations, and defaults to a 5-second interval. The MCP client must retain cursors and make subsequent calls.
7. For native Weixin @mentions or unsupported attachment previews, use the returned Computer Use handoff only in the desktop containing Weixin. The MCP server cannot start Computer Use.

The tool catalog contains 29 endpoints; the capabilities response groups features into 27 labels, so those counts differ. continuous_message_listener is not implemented.

## Data and security

The server operates locally against the logged-in user's Weixin process and local stores. Some guarded actions use version-pinned in-process access, and file submission loads a native bridge into the Weixin process. Review [SECURITY.md](../../SECURITY.md), [the native bridge notes](../../native-src/README.md), [NOTICE](../../NOTICE), and [third-party read-store attribution](../../THIRD_PARTY_READSTORE.md) before use.

Conversation content returned to an AI client may be sent to that client's model provider under the client's own settings. Do not publish chat databases, message exports, account data, state directories, diagnostic logs, or screenshots. This project is unofficial and is not affiliated with Tencent or OpenAI.
