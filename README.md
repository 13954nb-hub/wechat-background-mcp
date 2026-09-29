# WeChat Background MCP

**Languages / 语言：** [简体中文](docs/zh-CN/快速开始.md) · [English](docs/en/GETTING_STARTED.md) · [界面与显示适配 / Interface and display](docs/zh-CN/界面与显示适配.md) / [EN](docs/en/INTERFACE_AND_DISPLAY.md)

**简体中文**

这是一个非官方、实验性的 Windows 本地 MCP 服务，连接用户已登录的微信桌面端。**2.5.2** 面向 Windows x64、CPython 3.12 x64 和经过完整指纹核验的微信 Windows **4.1.13.12 x64**，提供 29 个工具和 3 个 MCP 提示词。分辨率没有固定要求：界面操作会在每次调用时重新测量窗口、DPI、渲染区和目标控件；证据不足时停止。

[下载 2.5.2 安装包（Windows x64 wheel）](https://github.com/13954nb-hub/wechat-background-mcp/releases/download/v2.5.2/wechat_background_mcp-2.5.2-cp312-cp312-win_amd64.whl) · SHA-256：**95df0b182ba67f85664a592acba70a1c6c920e56f8b86cb9466759b4ee278cd7**

微信安装程序不随项目发布。请查看[官方 Windows 下载页](https://pc.weixin.qq.com/)及[安装与版本核验说明](docs/zh-CN/快速开始.md#微信客户端版本与下载)。官方安装链接可能更新到不兼容版本；不要仅凭文件名判断，也不要绕过版本校验。

**English**

This is an unofficial, experimental local MCP server for an already logged-in Windows Weixin client. **Version 2.5.2** targets Windows x64, CPython 3.12 x64, and the exact fingerprint-verified Weixin for Windows **4.1.13.12 x64**. It exposes 29 tools and 3 MCP prompts. There is no fixed monitor-resolution requirement: every UI action remeasures the window, DPI, render area, and target control, and stops when evidence is insufficient.

[Download the 2.5.2 Windows x64 wheel](https://github.com/13954nb-hub/wechat-background-mcp/releases/download/v2.5.2/wechat_background_mcp-2.5.2-cp312-cp312-win_amd64.whl) · SHA-256: **95df0b182ba67f85664a592acba70a1c6c920e56f8b86cb9466759b4ee278cd7**

The Weixin installer is not bundled. See the [official Windows download page](https://pc.weixin.qq.com/) and the [version and integrity notes](docs/en/GETTING_STARTED.md#weixin-client-version-and-download). Official download URLs can move to an incompatible build; do not rely on the filename or bypass the version check.

**Quick links / 文档**

- [简体中文：安装、首次部署与工具串联](docs/zh-CN/快速开始.md)
- [English: Installation, first deployment, and workflows](docs/en/GETTING_STARTED.md)
- [简体中文：微信界面、窗口和动态分辨率要求](docs/zh-CN/界面与显示适配.md)
- [English: Weixin interface, window, and dynamic display requirements](docs/en/INTERFACE_AND_DISPLAY.md)
- [29-tool catalog and workflows in English and Simplified Chinese](docs/TOOLS_AND_WORKFLOWS.md)

- [Capabilities and limitations](CAPABILITIES.md)
- [Security and data handling](SECURITY.md)
- [Disclaimer / 免责声明](DISCLAIMER.md)
- [Release notes](RELEASE_NOTES_2.5.2.md)

**简体中文功能概览：** 有界会话／消息读取与搜索、发话者方向证据、最多 16 个会话的客户端轮询、受控文字与附件提交、图片／文件查看交接。监听由 MCP 客户端分段调用，默认间隔 5 秒、单次最多 60 秒；服务端没有常驻监听或自行生成并发送回复的 daemon。

**Feature overview:** Bounded conversation/message reads and search, evidence-based sender roles, client-driven polling of up to 16 conversations, guarded text/attachment submission, and image/file viewing handoffs. Polling defaults to 5 seconds and each call is bounded to at most 60 seconds. The server is not a persistent listener or autonomous reply daemon.

The Python package is licensed under [Apache-2.0](LICENSE). This project is not affiliated with Tencent or OpenAI. Review [SECURITY.md](SECURITY.md), [NOTICE](NOTICE), and [THIRD_PARTY_READSTORE.md](THIRD_PARTY_READSTORE.md) before use. Do not publish chat databases, message exports, account data, logs, screenshots, or local state.

---

An **unofficial, experimental** local MCP server for a logged-in Windows Weixin desktop client. Version **2.5.2** exposes 29 tools for bounded conversation and message reads, selected-chat polling, search, and guarded local submission, plus hybrid MCP and Computer Use prompts. It is not affiliated with Tencent or OpenAI.

The single installable wheel contains the mandatory [Chinese first-use guide](src/wxbg/FIRST_USE_ZH.md) at `wxbg/FIRST_USE_ZH.md`. An AI installer can read it inside the wheel ZIP before installation; after connection, the `wechat_first_deploy_and_tool_chains` prompt returns its text. The [AI deployment notes](INSTALL_FOR_AGENTS.md) and [bilingual 29-tool workflow guide](docs/TOOLS_AND_WORKFLOWS.md) give source readers the same route.

This release targets **Windows x64 and Weixin 4.1.13.12 with one exact supported DLL hash**. Its UI operations depend on a minimized window and fresh, action-specific accessibility and geometry checks. A different client build, binary, unverified UI target, or account must fail closed; a matching version number alone is insufficient. The number of registered tools is not a claim that every tool has been verified end to end on every computer.

Use a separate CPython 3.12 x64 virtual environment for this release. A Python interpreter bundled with another AI app has not been validated for this package. The presence of `WeixinUpdate.exe` alone does not prove that an upgrade is active. If Weixin changes after setup, rerun the read-only DLL hash check with `wechat-mcp-configure`; an unsupported binary fails closed. Do not delete its updater or silently downgrade the customer's client.

The integration reads the local Weixin process and, during guarded operations, writes one version-pinned accessibility gate byte in that process before restoring its prior value. File attachment submission uses a Windows thread hook to load a native DLL into the Weixin process; after its first arm, that DLL remains resident until Weixin exits. These behaviors require the exact supported Weixin DLL SHA-256 and x64 layout/ABI, not just the displayed client version. See [SECURITY.md](SECURITY.md) and [native-src/README.md](native-src/README.md) before installing.

## Install with an AI assistant

Give the assistant a trusted local **2.5.2 wheel** and have it read `wxbg/FIRST_USE_ZH.md` inside that wheel. Do not assume a repository URL is available or current. The assistant must resolve the customer's own absolute paths, install into a CPython 3.12 x64 virtual environment, run `wechat-mcp-configure --list-running` and `--weixin-exe`, back up and edit only the requested MCP client entry, then restart the client and call `wechat_capabilities` and `wechat_status` for first-use calibration. Status does not navigate, send, or edit drafts, but it may briefly lease and restore the version-pinned in-process access gate; require `cleanup.restored=true`.

Keep the state directory stable across restarts and outside the source tree. The server's main Weixin window must be logged in, minimized, without an interfering popup, and discoverable from the **same interactive Windows desktop** as the MCP process. `wechat_status.layout_calibration` observes this machine's current window and controls; every geometry-dependent UI action remeasures its own target and fails closed when evidence is insufficient. Monitor resolution alone does not establish a safe coordinate. Never retry `outcome_unknown` automatically.

The server reads `WXBG_WEIXIN_EXE`, `WXBG_WEIXIN_DLL`, `WXBG_STATE_DIR`, and optional `WXBG_DATA_ROOT` from its launch environment. The installer emits those settings for your computer. The state directory must remain stable across restarts because it holds recovery and duplicate-prevention records. Do not put it inside this Git repository.

## Finding groups after membership changes

Use `wechat_search(keyword="name or known group key", scope="groups")`. A new call with no cursor opens a fresh authenticated snapshot and searches `contact` group entries absent from SessionTable, then SessionTable entries including hidden rows. It filters database keys ending in `@chatroom` and labels each result as a **candidate** with `membership_unverified: true` and its source. Both tables may lag a join, leave, or rename; a retained row does not prove current membership. After a change, start a new search with `cursor=None` rather than continuing an old cursor. Verify the current Weixin UI identity before opening or sending to a group. A database `conversation_key` is never a sendable UI `session_ref`.

## Hybrid operation and native mentions

MCP clients can discover the `wechat_hybrid_computer_use` prompt for a step-by-step handoff. Every guarded MCP read and UI action first discovers the Weixin main window on the MCP process's interactive desktop. If it returns `TARGET_NOT_VISIBLE`, those MCP operations are unavailable until both run on the same desktop. A Computer Use controller may see Weixin on another desktop, but its screenshot does not satisfy the MCP check. Use Computer Use only in the desktop containing Weixin, and do not send input from both controllers at once. If a database read returns `readstore_index_invalid`, stop database reads and use a supported UI observation only when its desktop and identity checks pass; do not fabricate a WAL index or modify Weixin's database sidecars.

`wechat_send_at_username` now returns a **structured Computer Use handoff** with `sent: false`; it does not send literal `@name` text, invoke Computer Use, or write a send journal record. `username` is required, while `session_ref` and conversation title/key are optional, unverified hints. This works as a request for a handoff even if MCP cannot discover a UI session, but completing it requires an MCP client with Computer Use access to the desktop containing Weixin. The client must verify the current group and exact member suggestion, select that member, and inspect the native token and complete unsent draft before one authorized Send action. Remarks and group nicknames may differ, including similar-looking characters. Do not use Down or Enter in the mention menu because those keys may type or send unexpectedly. If Computer Use is unavailable, the target is ambiguous, or the outcome becomes unknown, stop and report the state without an automatic retry. A local bubble does not prove remote delivery. See [CAPABILITIES.md](CAPABILITIES.md) and [INSTALL_FOR_AGENTS.md](INSTALL_FOR_AGENTS.md).

## Selected-chat monitoring and AI replies

`wechat_watch_new_messages` polls 1–16 user-selected database conversations per call. Before monitoring begins, the AI client uses the user's stated total duration in seconds or minutes, or asks once for it if omitted; there is no preset total duration. First call `wechat_read_new_messages(start_from="now")` for one exact conversation to obtain `account_epoch` and its cursor; use that epoch with `wechat_batch_read_messages` to create `start_from="now"` cursors for the remaining selected rooms. This anchors at current rows without returning old messages. Each watch call supplies an integer `wait_seconds` from 1 to 60, retains every returned room cursor, and repeats in the active AI client until the requested total duration has elapsed. The user may also set the detection interval to 1–60 seconds (`poll_interval_ms=1000–60000`); when omitted, the interval defaults to 5 seconds (5000 ms). This is a target between reads, so a bounded read may take longer and extend total wall time beyond the requested window. The `wechat_multi_chat_reply_loop` prompt describes the client loop and per-conversation pause rules.

The same duration rule applies to `wechat_wait_for_ui_hint`: use a duration already provided by the user or ask once before waiting, then supply an integer `timeout_seconds` from 1 to 60 on each call and repeat in the active client when a longer total duration was requested. A UI hint signals generic interface activity, not a verified new message; a timeout does not prove that no message arrived. This event wait has no detection-interval setting. Check the database cursor or the live Weixin UI for message evidence.

Message results use `sender_role` (`self`, `other`, `unknown`), `sender_role_verified`, and `sender_role_evidence`. Database rows map the shard-local `sender_id` through that same message shard's `Name2Id.rowid`, then compare the exact identity against the authenticated account and exact conversation key. Group rows additionally require a complete, bounded membership join from the same contact-store snapshot (`chat_room` → `chatroom_member` → `contact`). These produce `database_account_match`, `database_direct_contact_match`, or `database_group_member_match`. Unsupported schema, absent or incomplete group roster, ambiguous identities, and unmapped IDs stay `unknown`. Only the currently exposed rows from `wechat_read_messages` can use visible UI direction, based on the unique nested text element's position within the current message frame; missing, clipped, ambiguous, middle-position, or changed geometry stays unknown. A separate UI observation cannot promote a database watch row by matching its text, time, or position. Only `other` with `sender_role_verified: true` may enter an automatic-reply candidate set; ignore verified `self` and pause that room on `unknown`.

A successful MCP text/file/image send reports `sender_role: "self"`, `sender_role_verified: true`, and `sender_role_evidence: "local_outgoing_operation"` for that **local operation only**; it cannot be joined to a later database row and is not a remote-delivery receipt. Verify the exact selected target and content before replying to an eligible verified row. The client may use guarded MCP text send with a fresh, matching UI ref, or Computer Use on the Weixin desktop when that route needs GUI verification. A room whose identity, direction, or send result is uncertain pauses while independently healthy rooms continue. A batch-level read or cursor error pauses the affected batch until read-only per-room checks identify what remains valid. This is bounded client-driven polling, not a server daemon or guaranteed real-time delivery. The MCP server never generates or sends a reply on its own.

If a watched text row is unavailable or truncated, verify the matching message in the live Weixin UI; `wechat_get_attachment` does not read text rows. For an image/file row, resolve supported downloaded bytes with `wechat_get_attachment`; otherwise use the Computer Use viewing handoff and verify the live preview. Image content does not establish who sent the message: verify the exact live UI row is from the other person before replying, and pause the room if direction or content remains unknown.

## Viewing images and files

For an already-downloaded attachment with exact database message identity, `wechat_get_attachment` privately locates the matching cache candidate under the account's `msg\attach\<chat MD5>\<YYYY-MM>\Img` directory, decodes it, and checks the message-declared size and MD5 before returning bytes. The raw encrypted `.dat` path is neither a usable image nor a public output path. Supported static PNG, JPEG, and WebP images up to 4 MiB are also returned as inline MCP `ImageContent`, alongside the short-lived in-memory resource link. An AI client with image vision can inspect that content; the MCP server has no built-in OCR or image-understanding model. Larger verified images remain available through the resource link without inline image content. Clients that cannot consume the image or resource can use `wechat_view_attachment_handoff` and Computer Use to find the exact chat and message on the Weixin desktop and inspect a supported in-app preview. `wechat_read_file_card` observes a file card but does not open or read it. The server cannot launch Computer Use. A screenshot of a preview is visual evidence, not verified original bytes. Do not use a preview to bypass account changes, ambiguous message identity, or changed file paths; never execute files or macros as part of viewing.

## Limits and data handling

- Database reads are bounded quiet-window observations, not atomic full-history exports. UI reads cover only supported observable views. See [CAPABILITIES.md](CAPABILITIES.md).
- A local submission does not prove remote delivery. Attachment and image formats and sizes are limited; payments and account changes are excluded.
- The server works locally. Conversation content passed to an AI client may be sent to that client's model provider under the client's settings. Keep diagnostic logs and state outside the repository; never commit real chat data.
- This project is not an official Weixin integration. Review the applicable [Tencent terms](https://weixin.qq.com/cgi-bin/readtemplate?head=true&lang=zh_CN&s=default&t=weixin_agreement) and obtain any permission required for your use or distribution.

## Source, license, and security

The Python source is in `src/wxbg`; the native bridge source is in `native-src`, with packaged native artifacts in `src/wxbg/native`. Build instructions are in [native-src/README.md](native-src/README.md). Licensed under [Apache-2.0](LICENSE); see [NOTICE](NOTICE) and [THIRD_PARTY_READSTORE.md](THIRD_PARTY_READSTORE.md) for attribution. Report security issues privately as described in [SECURITY.md](SECURITY.md).

The packaged native source hashes do not match the historical binary build receipt. A bit-identical rebuild of the packaged attachment DLL from this source has not been established; the source and packaged DLL should not be treated as a proven reproducible build pair.
