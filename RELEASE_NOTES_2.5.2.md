# WeChat Background MCP 2.5.2

**Release date:** 2026-09-29
**Platform:** Windows x64, CPython 3.12 x64
**Supported client:** Weixin for Windows 4.1.13.12 x64 with exact DLL hash validation

## English

### Download

- [Windows x64 wheel](https://github.com/13954nb-hub/wechat-background-mcp/releases/download/v2.5.2/wechat_background_mcp-2.5.2-cp312-cp312-win_amd64.whl)
- Size: 554,022 bytes
- SHA-256: **95df0b182ba67f85664a592acba70a1c6c920e56f8b86cb9466759b4ee278cd7**
- Weixin client installers are not redistributed. See the [official download and version notes](docs/en/GETTING_STARTED.md#weixin-client-version-and-download).

### Changes

- Correct minimized-window layout calibration while retaining strict root/render dimension and native-origin checks.
- Classify message sender direction only from exact account/contact evidence or the current visible UI row; ambiguous or unsupported cases remain unknown.
- Keep per-action accessibility, target, geometry, clipping, identity, and draft checks.
- Publish the matching native bridge manifest and 2.5.2 deployment guide inside the wheel.
- Add bilingual first-deployment, interface, dynamic display, security, and disclaimer documentation.

### Verification

- Source suite: 1,263 passed, 2 dependency warnings, 1,084 subtests.
- Clean installed-wheel suite: 1,263 passed, 2 dependency warnings, 1,084 subtests.
- Synthetic geometry: 90 cases across window sizes, DPI scales, and desktop origins; the below-minimum layout failed closed.
- Post-restart live probe: five safe metadata/status/synthetic no-match calls passed; no real conversation content was read and no message or attachment was sent.

The 29 tool endpoints and 3 prompts are not a claim that all operations were exercised live. Real message reads, monitoring, sends, and attachment viewing were not part of this release check. Read the [bilingual disclaimer](DISCLAIMER.md), [installation guide](docs/en/GETTING_STARTED.md), and [interface guide](docs/en/INTERFACE_AND_DISPLAY.md).

## 简体中文

### 下载

- [Windows x64 wheel 安装包](https://github.com/13954nb-hub/wechat-background-mcp/releases/download/v2.5.2/wechat_background_mcp-2.5.2-cp312-cp312-win_amd64.whl)
- 文件大小：554,022 字节
- SHA-256：**95df0b182ba67f85664a592acba70a1c6c920e56f8b86cb9466759b4ee278cd7**
- 不再分发微信客户端安装程序。请查看[官方微信下载与版本说明](docs/zh-CN/快速开始.md#微信客户端版本与下载)。

### 更新内容

- 修正微信最小化时的窗口几何校准，同时继续严格核对根窗口／渲染区尺寸及原生窗口原点。
- 增加有证据约束的发话者识别：仅根据精确账号／联系人证据或当前可见 UI 消息行判断方向；模糊或不支持的情况保持为 unknown。
- 保留每次操作各自的 UI 可访问性、目标、几何、剪裁、身份和草稿检查。
- wheel 内附匹配的原生桥接清单和 2.5.2 首次部署指南。
- 补充首次部署、界面、动态显示、安全和免责声明双语文档。

### 验收

- 源码完整测试：1,263 项通过，2 条依赖警告，1,084 个子测试。
- 干净安装 wheel 后完整测试：1,263 项通过，2 条依赖警告，1,084 个子测试。
- 合成几何测试：窗口尺寸、DPI 和桌面原点组合共 90 项；低于最低尺寸时按安全规则停止。
- 重启后的实时探测：5 项安全状态／元数据／合成无匹配调用通过；未读取真实聊天内容，也未发送消息或附件。

工具目录列出 29 个端点和 3 个提示词，并不表示所有操作都已实机调用。此次发布检查未读取真实消息、启动监听、发送消息或查看真实附件。请阅读[双语免责声明](DISCLAIMER.md)、[安装指南](docs/zh-CN/快速开始.md)和[界面指南](docs/zh-CN/界面与显示适配.md)。
