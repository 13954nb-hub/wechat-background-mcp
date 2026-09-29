# Disclaimer / 免责声明

**Languages / 语言：** [简体中文](#简体中文) · [English](#english)

## 简体中文

微信 Background MCP 是一个**非官方、实验性**项目，与腾讯、微信、Weixin、OpenAI 或任何 MCP 客户端厂商均无隶属、背书或代理关系。项目名称和相关商标归各自权利人所有。

本软件按 Apache License 2.0 提供，不提供任何明示或默示担保，包括适销性、特定用途适用性、准确性、可用性、安全性或不侵权担保。使用者须自行评估并承担安装、配置、运行、更新、数据丢失、账号限制、服务中断及其他风险。适用范围以仓库中的许可证和 NOTICE 为准。

本版本只对文档列明的 Windows x64、CPython 3.12 x64 和微信 Windows 4.1.13.12 x64 精确二进制指纹作过校验。版本号相同不代表二进制相同；版本不匹配时必须停止。项目不保证其他微信版本、系统、主题、显示器、缩放比例、虚拟桌面或远程桌面可用。窗口布局测试不等于对所有设备进行实机认证。

服务可在本机读取用户授权使用的微信进程和数据，并通过 MCP 将用户要求的内容返回给 AI 客户端。AI 客户端及模型服务商可能依其各自设置和条款处理这些内容。账号所有者须负责取得聊天参与者所需的授权、遵守隐私和数据保护规则，并自行保管账户、数据库、状态和备份。请勿公开上传聊天记录、数据库、凭据、个人信息、日志或截图。

消息、文件或图片工具返回本地操作结果，不构成微信服务器已收到、对方已看到或已被通知的证明。消息身份、方向、群成员、附件预览、界面目标或操作结果不明确时，应停止操作；不得盲目重发。有限轮询不保证实时送达，服务也不会自行常驻监听或保证 AI 回复质量。

本软件不是微信官方 API 或受支持的微信自动化接口。使用前请阅读微信／Weixin 的当前服务条款与政策。仅可在你拥有或获明确授权使用的账号、会话和数据上运行；不得用于未经授权的监控、隐私侵犯、规避访问控制、骚扰、垃圾信息或违法用途。你须自行确认此工具的使用与分发符合所在地法律、平台规则及第三方许可证。

Weixin 安装程序不由本项目打包或再分发。请使用腾讯或腾讯授权来源，并自行核验来源、签名和版本。本项目维护者不为第三方下载站、客户端更新内容或外部链接负责。

在法律允许的最大范围内，项目作者和贡献者不对因使用、无法使用或依赖本项目造成的直接、间接、附带、特殊、惩罚性或后果性损失承担责任。任何使用均由使用者自行决定并承担责任。

## English

WeChat Background MCP is an **unofficial, experimental** project. It is not affiliated with, endorsed by, or acting as an agent of Tencent, WeChat, Weixin, OpenAI, or any MCP client vendor. Product names and trademarks belong to their respective owners.

The software is provided under the Apache License 2.0, without warranties of any kind, express or implied, including merchantability, fitness for a particular purpose, accuracy, availability, security, or non-infringement. You evaluate and accept the risks of installation, configuration, operation, updates, data loss, account restrictions, service interruption, and other use. The repository license and NOTICE govern the applicable terms.

This release has been validated only for the exact binary fingerprint documented for Windows x64, CPython 3.12 x64, and Weixin for Windows 4.1.13.12 x64. A matching version label does not prove a matching binary; stop on any mismatch. The project does not promise compatibility with other Weixin builds, operating systems, themes, displays, scaling settings, virtual desktops, or remote desktops. Synthetic window-layout tests are not certification on every device.

The server can read a locally available Weixin process and data used by an account owner and return requested content to an AI client over MCP. That client and its model provider may process the content under their own settings and terms. The account owner is responsible for obtaining any required participant authorization, complying with privacy and data-protection rules, and securing account data, databases, state, and backups. Do not publish chat history, databases, credentials, personal information, logs, or screenshots.

Results from message, file, or image tools report local operation evidence; they do not prove that Weixin's servers received the content, that a recipient saw it, or that a notification was delivered. Stop when message identity, direction, group membership, attachment preview, UI target, or operation outcome is unclear; never blindly resend. Bounded polling is not guaranteed real-time delivery, and the server does not autonomously run a persistent listener or guarantee AI reply quality.

This software is not an official Weixin API or a supported Weixin automation interface. Read the current WeChat/Weixin terms and policies before use. Operate only on accounts, conversations, and data that you own or are explicitly authorized to access. Do not use it for unauthorized surveillance, privacy violations, access-control circumvention, harassment, spam, or unlawful activity. You are responsible for determining whether your use and distribution comply with local law, platform rules, and third-party licenses.

The Weixin installer is not packaged or redistributed by this project. Obtain it from Tencent or a source Tencent authorizes, and independently verify its source, signature, and version. The maintainers are not responsible for third-party download sites, client updates, or external links.

To the maximum extent permitted by law, the authors and contributors are not liable for direct, indirect, incidental, special, exemplary, or consequential damages arising from the use of, inability to use, or reliance on this project. Use is at your own discretion and risk.
