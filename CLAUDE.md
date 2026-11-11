# BladeSDR — 給 Claude Code 的專案說明

BladeRF 2.0 micro xA4 頻譜監控工具：網頁監控（即時 / 分區動態 / 掃描結果 / 回放 / 事件）、掃描腳本、響應校正。
詳細說明見 `README.md`（功能、架構、經驗、已知限制）與 `CONTRIBUTING.md`（協作流程）。使用者以繁體中文溝通，請用繁體中文回覆。

## 硬規則
- **只接收，不發射**。不要寫任何 TX 程式。Bias-T 一律強制關閉，不要提供開啟的控制。
- BladeRF 同一時間只能被**一個程式**使用（Mac 與 Windows VM 也不能同時用）。要用裝置前先確認沒有殘留的 server / `live_rx.py` / `hopper.py` / `bladeRF-cli`。
- 不提交量測資料、錄製檔、`webmon/config.json`、`events.csv`、`.venv/`、`sdrpp-src/`、金鑰。
- 電平是 dBFS，沒有 dBm 校正；不要把 dBFS 當 dBm 對外講。

## 架構重點
- `common/bladerf_io.py`：libbladeRF 的 ctypes 封裝（`Radio`、`load_lib`、`watch_stdin`）。各平台找函式庫，`BLADERF_LIB` 環境變數可覆寫。
- `webmon/server.py`：伺服器與模式狀態機；子程序 `webmon/live_rx.py`、`webmon/hopper.py`、`sweep/bladerf_sweep.py`。
- 乾淨停止子程序的方式：**關閉子程序 stdin** → 等待 → `terminate` → `kill`（不用 SIGINT / FIFO / `select` 管道，為了跨平台）。
- 前端全部在 `webmon/static/index.html`（純 JS + Canvas，無外部函式庫）。
- 啟動：`cd webmon && ../sweep/.venv/bin/python server.py --port 8080`（Windows 路徑為 `..\sweep\.venv\Scripts\python.exe`）。

## 踩過的坑
- FPGA 調諧模式要先設，再設增益。串流進行中不可切換調諧或增益模式（會卡住），要先停。
- 專案路徑含空格；Windows 上也要注意引號與反斜線。
- 檔案讀寫一律明確指定 `encoding="utf-8"`。

## 目前狀態與待辦：Windows 支援（分支 `feat/windows-support`）
共用層重構（PR #1）已合併，**Mac 上驗證過，Windows / Linux 完全沒驗證**。在 Windows 上請依序驗證，並把每一步的實測結果寫進 PR：

1. 前置：Python 3.11+、Nuand Windows 安裝包（`bladeRF.dll`、WinUSB 驅動）、Parallels 把 bladeRF 的 USB 指派給 Windows。
2. `python -c "from common.bladerf_io import load_lib; print(load_lib())"` 能載入 DLL（找不到就用 `BLADERF_LIB`；注意 `os.add_dll_directory`）。
3. 開啟 Radio、設頻率、收 1 秒資料。
4. 啟動 `webmon/server.py`，測即時監控的開始 / 停止（重複多次，確認無殘留 python 程序，停止耗時應為數十毫秒到數秒）。
5. 分區動態監控、錄製與回放。
6. `--fast` 掃描（例如 400–600 MHz）與 Mac 結果比對形狀。
7. 沒接裝置時網頁仍能開（回放、掃描結果、事件）。
8. 補 Windows 的啟動腳本（`.bat` 或 PowerShell）與 README 的 Windows 章節。

待決定：完整（非 fast）掃描目前仍用 `bladeRF-cli` 逐步擷取，要沿用或改走共用層（改了需重新驗證掃描結果）。
已知遺留：server 被結束時可能留下 `live_rx.py` / `hopper.py` 子程序；長時間穩定性（USB 掉線、停止後重啟卡住）只測過短循環。

## 協作
- `main` 只經由 PR 合併；分支 `feat/ fix/ docs/ chore/ refactor/`；Conventional Commits；PR 照 `.github/pull_request_template.md` 填，沒驗證的項目要明寫。
- Windows 與 Mac 各自用分支，不共用工作目錄。
