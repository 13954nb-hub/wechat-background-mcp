# Weixin interface and dynamic display guide

[简体中文](../zh-CN/界面与显示适配.md) · [Installation guide](GETTING_STARTED.md) · [Repository home](../../README.md)

Use this experimental integration only on an account and data you own or are explicitly authorized to access. Read the bilingual [Disclaimer](../../DISCLAIMER.md).

## What the integration connects to

    MCP client / AI
        → local stdio MCP server
        → logged-in Weixin process and its local data
        → verified, bounded results returned to the MCP client

The server does not operate a cloud WeChat account. Its local UI routes require the Weixin window to be discoverable from the same interactive Windows desktop session as the MCP process.

## Weixin window areas

| Visible area | How the server uses it |
| --- | --- |
| Main Weixin window | Establishes a unique, logged-in client process. Multiple matching main windows are ambiguous. |
| Conversation list | Supports guarded, fresh observations and candidate discovery. A hidden database row is not by itself a current UI identity. |
| Active conversation header | Helps verify the chat identity before a UI action. Recheck after navigation or window changes. |
| Message/render area | Supplies visible message and attachment observations when that UI route is supported and its geometry is verified. |
| Composer and Send button | Used only by supported guarded submission paths after target, control, and draft checks. A local result is not a remote delivery receipt. |

The account owner must sign in to Weixin. Keep the main window logged in and minimized for normal MCP operation, with popups and file dialogs closed. Do not operate Weixin simultaneously with the mouse, a second Computer Use controller, and MCP.

## Dynamic size, resolution, and DPI behavior

There is no single required monitor resolution. The runtime observes the current Weixin root window, render client, DPI scale, desktop origin, clipping, semantic controls, selected conversation, and target control. A UI action uses fresh measurements and stops if the window moved, dimensions do not agree, the target is clipped, or the evidence is ambiguous. It does not reuse coordinates measured on another PC or rely on a one-time proportional scale.

The 2.5.2 acceptance suite exercised a synthetic matrix of 90 geometry cases: six window/display sizes from 800×600 to 3840×2160, five DPI scale values from 100% to 250%, and three desktop origins, plus a below-minimum-size fail-closed case. These are automated geometry cases; they are not a claim that every monitor, Weixin theme, window state, or remote desktop has been tested. No physical display resolution was changed during acceptance.

On first setup, call wechat_status to inspect the machine's current layout_calibration, including the root rectangle and render client. This is an observation, not a permanent calibration. Every later UI action performs its own checks. If the Weixin window is on a different virtual desktop, hidden, duplicated, covered by a dialog, or has unverified geometry, stop that route and report the returned reason.

### Practical display setup

1. Use a normal, non-zero-size Windows desktop and keep the Weixin main window logged in.
2. Close transient dialogs. Avoid remote-desktop resize, display-scale changes, and window movement while a UI action is running.
3. Keep the MCP host and Weixin in the same interactive Windows session. Session 0 services and a different virtual desktop cannot satisfy this requirement.
4. Let wechat_status observe the current layout. Do not prescribe a fixed window size or manually edit coordinates.
5. After moving/resizing the window, changing DPI, or reconnecting a remote desktop, run the read-only status check again; each UI tool still measures its own target.
6. A fail-closed layout result means the particular UI action is unavailable. Do not bypass it by reducing integrity checks or repeatedly clicking guessed coordinates.

## Recommended tool chains

| Task | Sequence |
| --- | --- |
| First deployment | Read wechat_first_deploy_and_tool_chains → wechat_capabilities → wechat_status |
| Find a group after membership changes | Fresh wechat_search(scope="groups") → verify the exact live UI identity → use a current UI session reference |
| Read messages | Verify the exact chat → use a bounded read tool → distinguish self / other / unknown using verified evidence |
| Monitor selected chats | wechat_read_new_messages(start_from="now") to establish a cursor → wechat_watch_new_messages with retained cursors → repeat from the MCP client for the requested duration |
| Reply | Require an exact target and verified other sender → inspect the complete reply and unsent draft → use an authorized send path → check operation status when needed |
| Inspect an attachment | wechat_get_attachment for verified bytes → otherwise wechat_view_attachment_handoff and inspect the exact live Weixin preview with Computer Use |
| Native @mention | wechat_send_at_username returns a Computer Use handoff; it does not send the mention itself |

wechat_watch_new_messages is bounded polling, not push delivery or an always-running daemon. A call is limited to 1–60 seconds, can cover up to 16 selected chats, and uses a 5-second detection interval by default. Message direction or identity marked unknown must pause that chat's reply path.

## Troubleshooting

| Result | Meaning and next step |
| --- | --- |
| TARGET_NOT_VISIBLE | The MCP process cannot discover the main window on its current interactive desktop. Resolve the desktop/session issue before retrying UI routes. |
| TARGET_AMBIGUOUS | More than one candidate main window was discovered. Close or resolve duplicate client windows, then recheck. |
| unverified_layout | Current geometry or target evidence is insufficient. Stop that UI action and correct the window state. |
| readstore_index_invalid | Stop local database reads. Do not edit or reconstruct Weixin WAL/SHM files. |
| outcome_unknown | Preserve the operation ID and inspect status or fresh UI evidence. Never blindly resend. |

The registered catalog has 29 tool endpoints, while the capabilities response groups them into 27 feature labels. continuous_message_listener is not implemented.
