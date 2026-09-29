# AI First-Use Guide / AI 首次部署指引（2.5.2）

## English quick route

- Use this only on the Windows computer and MCP client selected by the account owner. The owner signs in to Weixin and remains responsible for the account and data. Never upload chat databases, exports, credentials, local state, logs, or screenshots.
- Follow the complete [English installation and first-deployment guide](docs/en/GETTING_STARTED.md), the [English interface and display guide](docs/en/INTERFACE_AND_DISPLAY.md), and the [bilingual 29-tool catalog and workflows](docs/TOOLS_AND_WORKFLOWS.md). The wheel contains `wxbg/FIRST_USE_ZH.md`; the `wechat_first_deploy_and_tool_chains` prompt returns that Chinese first-use guide.
- Use Windows x64, a dedicated CPython 3.12 x64 environment, and a trusted 2.5.2 wheel. Verify the published wheel SHA-256 before installation. Do not assume an AI application's bundled Python is compatible.
- Ask the user to choose the exact Weixin executable when several are present. Run `wechat-mcp-configure --list-running`, then pass the selected absolute path to `--weixin-exe`. The helper validates the installed DLL and prints configuration; it does not edit Weixin or the MCP client's settings. Back up and edit only the requested MCP entry.
- Keep the Weixin main window logged in, unique, minimized, free of dialogs, and on the same interactive Windows desktop as the MCP process. Run `wechat_capabilities`, then read-only `wechat_status`; require `cleanup.restored=true`. Every geometry-dependent action measures its live target again. Do not reuse fixed screen coordinates or alter display resolution to satisfy checks.
- Stop on a client hash mismatch, hidden or ambiguous window, invalid database index, unverified layout, uncertain identity, or `outcome_unknown`. Never patch the expected hash, modify Weixin database sidecars, or blindly retry a mutation. Use the bilingual tool guide to chain reads, monitoring, attachment handoffs, and authorized replies.

## 简体中文

只在使用者指定的 Windows 電腦和 MCP 客戶端設定中操作。使用者負責登入微信；勿上傳聊天資料、帳號資料庫、狀態檔、日誌或截圖。首次部署以**使用者提供且來源可信的本地 2.5.2 wheel 或原始碼**為起點；不要假設 GitHub 倉庫網址可用，也不要把未核實的 `git clone` 網址當作唯一安裝途徑。單一 wheel 內含必讀 `wxbg/FIRST_USE_ZH.md`，安裝前可當 ZIP 開啟閱讀；連接後 `wechat_first_deploy_and_tool_chains` 提示詞返回同一份指引。部署完成後按 [中英双语 29 工具目录与工作流](docs/TOOLS_AND_WORKFLOWS.md)選擇工作流程。

## 最短部署順序

1. 核對 Windows x64、獨立 **CPython 3.12 x64**、已登入的 Weixin **4.1.13.12**、MCP 進程與微信位於同一互動桌面。記錄本地安裝包來源，若附有 SHA-256 清單先核對。不要自行從第三方下載或降級微信；畫面版本號不能代替 DLL 校驗。
2. 在客戶自己的穩定路徑建立 venv，安裝本地 wheel；若只有可信原始碼，改用原始碼根目錄執行 `pip install <source-directory>`。不要沿用作者的絕對路徑或另一 AI 客戶端捆綁的 Python。

   ```powershell
   py -3.12 -m venv 'C:\path\to\wechat-mcp-venv'
   & 'C:\path\to\wechat-mcp-venv\Scripts\python.exe' -m pip install 'C:\trusted\wechat_background_mcp-2.5.2-cp312-cp312-win_amd64.whl'
   ```

3. 用**該 venv** 的設定器先列執行中客戶端，再對使用者選定的 `Weixin.exe` 絕對路徑輸出設定。多個候選時請使用者選定；不要掃遍磁碟或猜測安裝位置。設定器核對 DLL 大小、SHA-256、PE 佈局、Python 架構與已安裝套件版本，僅**輸出**設定，不修改微信或 MCP 客戶端。

   ```powershell
   & 'C:\path\to\wechat-mcp-venv\Scripts\wechat-mcp-configure.exe' --list-running
   & 'C:\path\to\wechat-mcp-venv\Scripts\wechat-mcp-configure.exe' --weixin-exe 'C:\actual\Weixin.exe' --format codex
   ```

   其他 MCP 客戶端用 `--format json`。僅在自動定位失敗時才指定 `--weixin-dll` 或 `--data-root` 的客戶電腦**絕對路徑**；後者仍須匹配目前登入帳號。可用 `--state-dir` 指定持久且所有該使用者 MCP 客戶端共用的目錄，保持在原始碼樹外。`--python` 只指向已安裝本版的 CPython 3.12 x64；`--launch` 僅供確有需要的原始碼啟動。
4. **先備份**使用者指定 MCP 客戶端的現有設定，只加入或更新這一個 MCP 條目，保留無關設定。確認命令是該 venv 的 `python.exe -m wxbg`，環境中的 `WXBG_WEIXIN_EXE`、`WXBG_WEIXIN_DLL`、`WXBG_STATE_DIR` 都是當機絕對路徑。重啟後先取得 `wechat_first_deploy_and_tool_chains`，呼叫 `wechat_capabilities` → **唯讀** `wechat_status` 作本機窗口初驗，查看 `layout_calibration` 的 `root_rect`、`render_client` 及控件／操作就緒觀察。若某條 UI 路線回 `unverified_layout`，停止該路線並檢查其語義錨點；資料庫工具仍有各自的硬性前置條件。若客戶端未重新載入工具，重啟後重新列工具。

## 微信視窗與顯示條件

微信須已登入並有**唯一主視窗**；主窗在正常使用時先**最小化**，關閉微信彈窗、檔案對話框和遮擋操作的提示，不要同時以滑鼠、MCP、Computer Use 操作。MCP 與微信必須在**同一 Windows 互動桌面**，否則即使 Computer Use 看得到微信，MCP 也可能回 `TARGET_NOT_VISIBLE`。有多個符合的主窗會回 `TARGET_AMBIGUOUS`。兩種情況都先處理桌面或視窗身份，再做受控讀取與操作。

`wechat_status.layout_calibration` 首次量取**這台電腦的** UIA 主窗邊界、渲染區及可辨識控件，並報告各操作的就緒情況。它是唯讀初驗，不是一次輸入後永久有效的座標設定。每次 UI 操作都重新觀察當前窗口、DPI／縮放、控件矩形、可見性、剪裁和選中會話；需要輸入時，把當前控件安全點換算到其渲染客戶區，並在提交前再次確認窗口與目標未變。缺少可靠控件、DPI／渲染換算失敗、窗口改變或原生橋接檢查不通過時，該操作安全停止。某工具就緒不代表其他工具就緒。

**顯示器解析度不是微信視窗矩形。**不要套用其他電腦的座標，不能只按比例縮放歷史坐標，也不要僅為通過檢查而更改顯示器解析度。微信窗口移動、縮放、DPI 改變、遠端桌面切換後，再執行一次 `wechat_status` 作唯讀觀察；隨後每條操作仍各自重新量測。`wechat_probe_session_container` 可提供特定列表的唯讀能力證據，但不產生可發送的 `session_ref`。資料庫工具仍須通過同桌面的主窗發現和帳號檢查。

## 失敗後的最小分流

| 回應 | AI 下一步 |
| --- | --- |
| DLL 大小／雜湊／PE 不符，或 Python 版本／架構不符 | 停止部署，報出實際路徑與檢查結果；不要改 hash、停用更新程式或默默降級客戶端。微信更新後重新執行設定器的唯讀校驗。 |
| `TARGET_NOT_VISIBLE`／`TARGET_AMBIGUOUS` | 停止受控 MCP 讀取與 UI 操作；核對同一互動桌面、登入狀態和唯一主窗。Computer Use 截圖不能替代 MCP 視窗發現。 |
| `readstore_index_invalid` | 停止資料庫讀取；只在 UI 守衛通過時採用受支援的 UI 觀察。不要產生或修改微信的 WAL／`-shm`。 |
| `unverified_layout`、目標／身份模糊、非空草稿 | 停止該 UI 操作，保留現場，重新核對身份或交由受支援的 Computer Use 客戶端處理。 |
| `outcome_unknown` | 若原操作有 `operation_id`，保留它並用 `wechat_operation_status` 唯讀查詢；若沒有（如 `wechat_open_session` 或 Computer Use），只核對新鮮 UI 狀態且保持結果未知。**不可換 ID、換控制器或盲目重發**。 |

原生 `@` 和無法讀取位元組的圖片／文件預覽只回傳 **Computer Use 交接**，MCP 伺服器不會替宿主呼叫 Computer Use。先讀 `wechat_hybrid_computer_use` 提示詞；AI 客戶端必須在微信所在桌面重新核對目標與內容，避免兩個控制器並行輸入。多會話監聽前讀 `wechat_multi_chat_reply_loop`；監聽是 AI 客戶端持續呼叫的有界輪詢，**不是常駐 daemon**。
