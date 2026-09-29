# Security
## 简体中文摘要

此项目处理本机微信进程与敏感聊天数据。受控操作会短暂写入一个版本锁定的进程内访问门字节并尝试还原；文件提交会用 Windows 线程钩子把原生桥接 DLL 加载到微信 UI 进程。桥接 DLL 首次启用后会一直留在微信进程中，直到微信退出；关闭 MCP 服务不会将其卸载。仅支持文档所列微信构建与精确 DLL 哈希。请把状态放在仓库外并限制本机账户访问，绝不要在公开 issue 附上数据库、日志、截图、账户信息或状态文件。漏洞请用 GitHub 私密安全公告报告。详细说明见[English security notes](SECURITY.md)和[双语免责声明](DISCLAIMER.md)。

This project handles sensitive local conversations and uses a version-pinned desktop integration. Use it only on a machine and account you control. Keep its persistent state outside the repository and restrict access to the local user. Never attach real chat databases, logs, screenshots, or state journals to public issues.

The controller opens the local Weixin process and uses a guarded `WriteProcessMemory` call to set one pinned accessibility gate byte, then attempts to restore the original byte and records recovery state. File attachment submission uses `SetWindowsHookExW` to load a native bridge DLL into Weixin's UI thread. The bridge temporarily changes a pinned import-address-table slot; after its first arm, the DLL stays resident until the Weixin process exits. Closing the MCP server does not unload that resident module.

These operations depend on the exact supported Weixin 4.1.13.12 `Weixin.dll` SHA-256 (`e3240bf8a4d00593a4b3e6ce6c8b6ac26897622c27f410f6655c4eee17cb3b6d`), x64 PE layout, code signature, ABI, and supported UI geometry. Other binaries or layouts are unsupported. The native source hashes differ from the historical build receipt, and a bit-identical rebuild of the packaged bridge DLL has not been established.

To report a vulnerability, open a private GitHub security advisory for this repository. Include a minimal synthetic reproduction and the supported client build; do not include live conversation content or credentials.

Unsupported client binaries, ambiguous account or path identity, invalid DLL hashes, and unknown operation outcomes must fail closed. The project does not provide a remote service or a guarantee of message delivery to recipients.
