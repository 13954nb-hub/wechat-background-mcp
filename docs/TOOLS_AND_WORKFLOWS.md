# Tool catalog and workflows / 工具目录与工作流

**Languages / 语言：** [English](#english) · [简体中文](#简体中文)

## English

### 2.6.0 tool catalog

The server registers 29 tools and 3 prompts. A catalog entry or passing automated test does not mean the tool was exercised against live private data.

| # | Tool | Purpose | 2.6.0 verification |
| ---: | --- | --- | --- |
| 1 | wechat_batch_read_messages | Read a bounded page from 1–16 selected conversations using retained cursors. | Source and clean-wheel suites; no live private read. |
| 2 | wechat_capabilities | Report package/client versions, tool catalog, capabilities, and limits. | Safe live metadata probe passed. |
| 3 | wechat_get_attachment | Return verified bytes for a supported, already-downloaded attachment. | Source and clean-wheel suites; no private attachment read. |
| 4 | wechat_get_draft | Read the current unsent draft. | Source and clean-wheel suites; no live draft read. |
| 5 | wechat_list_contacts | Observe a bounded address-book view. | Source and clean-wheel suites; no live contact read. |
| 6 | wechat_list_conversations | Page through local conversation candidates. | Source and clean-wheel suites; no live private list read. |
| 7 | wechat_list_sessions | List conversations currently exposed by the Weixin UI. | Safe live synthetic no-match probe passed. |
| 8 | wechat_open_session | Open a freshly verified UI session reference; this may mark messages as read. | Source and clean-wheel suites; no live navigation. |
| 9 | wechat_operation_status | Read the journal state for an existing operation ID; does not replay it. | Source and clean-wheel suites; no live mutation. |
| 10 | wechat_probe_session_container | Make a read-only probe for one exact, currently hidden session-list container. | Safe live synthetic probe passed. |
| 11 | wechat_read_contact_span | Read a bounded span of address-book views. | Source and clean-wheel suites; no live contact navigation. |
| 12 | wechat_read_file_card | Observe a visible file-card state; does not read the file contents. | Source and clean-wheel suites; no live file-card read. |
| 13 | wechat_read_history_span | Read a bounded span of nearby history views. | Source and clean-wheel suites; no live history navigation. |
| 14 | wechat_read_inbox | Read a bounded inbox or unread summary. | Source and clean-wheel suites; no live inbox read. |
| 15 | wechat_read_messages | Read currently visible messages and attach sender-direction evidence when available. | Source and clean-wheel suites; no live private message read. |
| 16 | wechat_read_new_messages | Read incremental messages for one exact conversation and cursor. | Source and clean-wheel suites; no live private message read. |
| 17 | wechat_scan_open_session | Search a bounded set of UI list views and open only a unique exact title. | Source and clean-wheel suites; no live navigation. |
| 18 | wechat_scan_session_viewports | Observe a bounded number of conversation-list viewports. | Source and clean-wheel suites; no live scrolling. |
| 19 | wechat_scroll_messages | Scroll a selected conversation by a bounded number of steps. | Source and clean-wheel suites; no live history navigation. |
| 20 | wechat_search | Search bounded conversation, group-candidate, or supported message text. | Safe live synthetic no-match probe passed. |
| 21 | wechat_send_at_username | Return a Computer Use handoff for selecting a native Weixin mention; does not send. | Source and clean-wheel suites; no live mention action. |
| 22 | wechat_send_file | Submit a supported small local file through guarded UI handling. | Source and clean-wheel suites; no live file send. |
| 23 | wechat_send_image | Submit a supported small local PNG through guarded UI handling. | Source and clean-wheel suites; no live image send. |
| 24 | wechat_send_text | Submit text to an exact, verified UI session through guarded handling. | Source and clean-wheel suites; no live message send. |
| 25 | wechat_set_draft | Set a draft only after comparing its expected current text. | Source and clean-wheel suites; no live draft mutation. |
| 26 | wechat_status | Observe client/account checks, layout calibration, and operation readiness. | Safe live status probe passed; gate restoration was confirmed. |
| 27 | wechat_view_attachment_handoff | Return unverified location hints for a Computer Use attachment-view flow. | Source and clean-wheel suites; no live attachment preview. |
| 28 | wechat_wait_for_ui_hint | Wait a bounded interval for generic Weixin UI activity; not proof of a message. | Source and clean-wheel suites; no live wait. |
| 29 | wechat_watch_new_messages | Poll up to 16 selected conversations for a bounded window of 1–60 seconds. | Source and clean-wheel suites; no live monitoring. |

The catalog counts endpoints; the capabilities response groups functions into 27 labels. They are different counts. The continuous_message_listener capability is not implemented.

### Recommended tool chains

| Goal | Sequence |
| --- | --- |
| First deployment | Read wechat_first_deploy_and_tool_chains → wechat_capabilities → wechat_status; require cleanup.restored=true. |
| Find a group after membership changes | Start a fresh wechat_search(scope="groups") → verify the live group identity → obtain a current UI session reference. |
| Read messages | Verify the exact chat → make a bounded read → use sender_role and sender_role_verified; pause on unknown. |
| Monitor multiple chats | Establish a start_from="now" cursor with wechat_read_new_messages → initialize other rooms with wechat_batch_read_messages → repeat bounded wechat_watch_new_messages calls from the active MCP client, retaining every cursor. |
| Reply to an incoming message | Require exact target and sender_role=other with sender_role_verified=true → inspect the full unsent content → use an authorized send path → check operation status if the result is uncertain. |
| Inspect media | Use wechat_get_attachment for verified bytes → otherwise use wechat_view_attachment_handoff in a Computer Use client on the Weixin desktop. |
| Select a native @mention | Use wechat_send_at_username as a handoff only; the Computer Use client must verify the exact group, member suggestion, native token, and unsent draft before an authorized send. |

Database sender identity is trusted only when the shard-local sender ID maps to an exact account/contact identity; a group sender additionally requires a complete bounded roster snapshot. Unsupported or ambiguous identities remain unknown. A visible UI sender-side observation belongs only to that observed UI row and cannot be copied to a database row by matching text or time. The server does not generate replies or stay running as a listener. Polling is client-driven and has no guaranteed real-time or exactly-once delivery.

## 简体中文

### 2.6.0 工具目录

服务登记 29 个工具和 3 个提示词。工具出现在目录中或自动化测试通过，不代表已对真实私人数据实机操作。

| 序号 | 工具 | 用途 | 2.6.0 验证情况 |
| ---: | --- | --- | --- |
| 1 | wechat_batch_read_messages | 使用保留游标，有界读取 1–16 个所选会话。 | 源码与干净 wheel 测试通过；未实读私人消息。 |
| 2 | wechat_capabilities | 报告程序／微信版本、工具目录、能力和限制。 | 安全的实时元数据探测通过。 |
| 3 | wechat_get_attachment | 返回已下载且校验通过的受支持附件字节。 | 源码与干净 wheel 测试通过；未读私人附件。 |
| 4 | wechat_get_draft | 读取当前未发送草稿。 | 源码与干净 wheel 测试通过；未实读草稿。 |
| 5 | wechat_list_contacts | 有界观察通讯录视图。 | 源码与干净 wheel 测试通过；未实读联系人。 |
| 6 | wechat_list_conversations | 分页列出本机会话候选项。 | 源码与干净 wheel 测试通过；未读取真实会话列表。 |
| 7 | wechat_list_sessions | 列出微信界面当前暴露的会话。 | 安全的实时合成无匹配探测通过。 |
| 8 | wechat_open_session | 打开经过新鲜核对的 UI 会话引用；可能将消息标为已读。 | 源码与干净 wheel 测试通过；未实机导航。 |
| 9 | wechat_operation_status | 只读查询已有操作 ID 的日志状态，不重放操作。 | 源码与干净 wheel 测试通过；未实机修改。 |
| 10 | wechat_probe_session_container | 只读探测一个准确且当前隐藏的会话列表容器。 | 安全的实时合成探测通过。 |
| 11 | wechat_read_contact_span | 有界读取多个通讯录视图。 | 源码与干净 wheel 测试通过；未实机导航通讯录。 |
| 12 | wechat_read_file_card | 观察当前可见的文件卡片状态；不读取文件内容。 | 源码与干净 wheel 测试通过；未实读文件卡片。 |
| 13 | wechat_read_history_span | 有界读取附近的多段聊天历史视图。 | 源码与干净 wheel 测试通过；未实机导航历史。 |
| 14 | wechat_read_inbox | 有界读取收件箱或未读摘要。 | 源码与干净 wheel 测试通过；未实读收件箱。 |
| 15 | wechat_read_messages | 读取当前可见消息，并在证据充分时标注发话者方向。 | 源码与干净 wheel 测试通过；未实读私人消息。 |
| 16 | wechat_read_new_messages | 按准确会话及游标增量读取消息。 | 源码与干净 wheel 测试通过；未实读私人消息。 |
| 17 | wechat_scan_open_session | 有界检查多个列表视图，仅在标题唯一匹配时打开。 | 源码与干净 wheel 测试通过；未实机导航。 |
| 18 | wechat_scan_session_viewports | 有界观察多个会话列表视口。 | 源码与干净 wheel 测试通过；未实机滚动。 |
| 19 | wechat_scroll_messages | 有界滚动当前所选会话。 | 源码与干净 wheel 测试通过；未实机导航历史。 |
| 20 | wechat_search | 搜索有界会话、群聊候选或受支持的消息文本。 | 安全的实时合成无匹配探测通过。 |
| 21 | wechat_send_at_username | 返回 Computer Use 原生微信提及交接；工具本身不发送。 | 源码与干净 wheel 测试通过；未实机提及。 |
| 22 | wechat_send_file | 通过受控界面流程提交受支持的小型本地文件。 | 源码与干净 wheel 测试通过；未实发文件。 |
| 23 | wechat_send_image | 通过受控界面流程提交受支持的小型本地 PNG。 | 源码与干净 wheel 测试通过；未实发图片。 |
| 24 | wechat_send_text | 通过受控流程向准确且核实过的 UI 会话提交文字。 | 源码与干净 wheel 测试通过；未实发消息。 |
| 25 | wechat_set_draft | 仅在比对当前预期文本后设置草稿。 | 源码与干净 wheel 测试通过；未实机改草稿。 |
| 26 | wechat_status | 观察客户端／账号检查、界面校准和操作就绪状态。 | 安全的实时状态探测通过；确认访问门已还原。 |
| 27 | wechat_view_attachment_handoff | 为 Computer Use 查看附件流程返回未经核实的位置提示。 | 源码与干净 wheel 测试通过；未实机预览附件。 |
| 28 | wechat_wait_for_ui_hint | 有界等待一般微信界面活动；活动提示不等于收到消息。 | 源码与干净 wheel 测试通过；未实机等待。 |
| 29 | wechat_watch_new_messages | 在 1–60 秒单次窗口内轮询最多 16 个所选会话。 | 源码与干净 wheel 测试通过；未实机监听。 |

目录统计的是工具端点；能力响应把功能分成 27 个标签，两个数字不同。continuous_message_listener 功能尚未实现。

### 推荐工具串联

| 目标 | 调用顺序 |
| --- | --- |
| 首次部署 | 读 wechat_first_deploy_and_tool_chains → wechat_capabilities → wechat_status；确认 cleanup.restored=true。 |
| 群成员变动后找群 | 从头调用 wechat_search(scope="groups") → 在微信界面核对群身份 → 获取当前 UI 会话引用。 |
| 读取消息 | 核对准确会话 → 有界读取 → 检查 sender_role 和 sender_role_verified；方向 unknown 时暂停。 |
| 监听多个会话 | 用 wechat_read_new_messages 建立 start_from="now" 游标 → 用 wechat_batch_read_messages 初始化其他会话 → 活跃 MCP 客户端重复调用有界 wechat_watch_new_messages 并保留每个游标。 |
| 回复收到的消息 | 要求准确目标及 sender_role=other、sender_role_verified=true → 检查完整未发送内容 → 使用获准发送流程 → 结果不明时查询操作状态。 |
| 查看媒体 | 优先用 wechat_get_attachment 获取已核实字节 → 否则在微信桌面上的 Computer Use 客户端使用 wechat_view_attachment_handoff。 |
| 选择原生 @ 提及 | wechat_send_at_username 仅用于交接；Computer Use 客户端发送前须核对准确群、成员建议、原生标记和未发送草稿。 |

只有当分片内 sender ID 能映射到准确的账号／联系人身份时，数据库发话者方向才可信；群消息还需要完整且有界的群成员快照。结构不支持或身份不明确时保留 unknown。界面观察到的发话者方向只属于该条界面消息，不能仅凭文字或时间复制到数据库行。服务端不会生成回复，也不会作为常驻监听器运行。轮询由 MCP 客户端继续调用，不保证实时送达或恰好处理一次。
