# WeChat Background MCP 2.6.0

Integrated Windows x64 / CPython 3.12 release. The exact supported client remains Weixin 4.1.13.12 x64 with its existing full DLL fingerprint; no new client versions are authorized.

## 修复与整合

- 原生附件桥与 Python 观察路径一致识别两个明确的渲染类名；跨类重复窗口、进程不符、尺寸或原点不符仍停止。
- 最小化窗口不依赖已折叠的外层客户区尺寸，但必须核对保留渲染区、进程和当前操作几何。没有固定屏幕坐标或分辨率回退。
- 文件按钮与发送按钮使用有限、精确的简繁体标签集合；重复或未知按钮仍拒绝操作。
- 文件卡片支持“檔案”/“文件”和三行短卡片，仅允许明确的“微信电脑版”/“微信電腦版”尾行。未知状态不会冒充上传完成。
- 保留 29 工具、3 提示词、动态几何测量、有限读取和现有安全边界；独立安装包不含本地运行状态、私人发票或聊天资料。

## Fixes and integration

- Match the two explicitly supported render-window classes consistently across native and Python paths. Reject duplicate/mixed render children and PID, size or origin mismatches.
- Iconic outer-window dimensions are not reliable; retained render geometry and operation-bound evidence remain mandatory. No preset coordinates or universal-resolution fallback.
- Use finite exact simplified/traditional attachment and Send labels, retaining ambiguity rejection.
- Accept bounded short file cards with exact file labels and optional known desktop footer. Missing or unrecognized status stays unknown, not completed.
- Retain the 29-tool / 3-prompt public interface and existing safety gates.

## Validation and limits

The release gate is full Python regression testing, owned-process native lifecycle/proxy/IAT/grant tests, source and binary hash checks, clean-environment wheel installation, dependency consistency, installed-wheel regression testing and independent review. Package checksums are provided as the release asset `SHA256SUMS.txt`, avoiding a self-referential hash in embedded package metadata.

These are bounded automated and owned-fixture checks, not certification of every PC, monitor, DPI, Windows build or Weixin language. This release does not claim a live client submission, completed upload or remote receipt for its new native DLL. No real messages are sent by the package acceptance tests.

Completed before publication: 1,274 source tests and 1,274 clean installed-wheel tests, with zero failures/errors/skips; all 83 loaded package modules resolved to the clean installation. Dependency check and the 29-tool / 3-prompt catalog passed. Eight native targets compiled; owned bridge/proxy/IAT/IAT-fault/grant suites passed 71/40/8/12/8 checks respectively. A relocated native rebuild has a different hash; bit-identical reproducibility is not claimed. Known nonfatal Pydantic settings and COM-threading warnings remain. Sanitized results are available as release asset `VERIFICATION_2.6.0.json`.

If a previous attachment DLL is already resident, replacing the package does not replace that loaded module. Fully exit Weixin and restart the MCP client before calibrating the new version. Follow [first deployment](docs/en/GETTING_STARTED.md) / [安装说明](docs/zh-CN/快速开始.md); mismatched client fingerprints fail closed. Unknown outcomes must not be automatically resent.
