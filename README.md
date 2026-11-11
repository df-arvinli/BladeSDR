# BladeSDR 頻譜監控專案

BladeRF 2.0 micro xA4 在 macOS 上的頻譜監控工具：即時頻譜、全頻段掃描、分區動態監控（軟體跳頻）、干擾事件紀錄。目標是**挑選通訊頻段**與**分析無人機被干擾的原因**。

> 狀態：頻譜分析功能階段性完成（截至 2026-10-27）。**只做接收**，不提供任何發射功能。
> 所有電平皆為**相對 dBFS，不是校準過的 dBm**，只適合比較變化與相對強弱。

---

## 1. 環境

| 項目 | 內容 |
|---|---|
| 硬體 | Nuand BladeRF 2.0 micro **xA4**（USB 3.0，接收範圍約 70 MHz–6 GHz，單次最大約 56 MHz） |
| 韌體／FPGA | 韌體 **2.6.0**（已由 2.4.0 升級）；FPGA **0.16.0**（由主機載入，見下） |
| 主機 | macOS（Apple Silicon），Homebrew |
| 驅動 | `brew install libbladerf`（2025.10） |
| Python | 3.14（pyenv），虛擬環境 `sweep/.venv`，套件：`numpy`、`matplotlib` |
| SDR++ | 已從原始碼編譯（`sdrpp-src/SDR++.app`，含 bladeRF 模組），僅作為輔助觀察 |

### 一次性設定（已完成，重灌時參考）

1. `brew install libbladerf`
2. xA4 需要 xA4 專用 FPGA 影像。從 Nuand 官方下載 `hostedxA4-latest.rbf`，存成 `~/.config/Nuand/bladeRF/hostedxA4.rbf`，libbladeRF 開啟裝置時會自動載入（**並未燒進裝置**）。
3. 新版 FPGA（0.16.0）要求韌體 ≥ 2.6.0。韌體以 `bladeRF-cli -f bladeRF_fw_latest.img` 刷新，刷完要**拔插 USB 重新上電**才會生效。
4. `python3 -m venv sweep/.venv && sweep/.venv/bin/pip install numpy matplotlib`

---

## 2. 啟動

```bash
cd webmon
../sweep/.venv/bin/python server.py --port 8080
```

瀏覽器開 <http://localhost:8080>。伺服器**只綁 `127.0.0.1`，沒有登入驗證**；要開放給區網前必須先加驗證。

BladeRF **同一時間只能被一個程式使用**（網頁伺服器、SDR++、命令列互斥）。

---

## 3. 功能

網頁有四個分頁，背後是五種互斥的模式：`idle`（待機）、`live`、`hop`、`sweep`、`calib`。啟動其中一個會自動停掉另一個（掃描、校正會先停止即時接收與分區監控）。

### 3.1 即時監控（live）
單一頻率的即時頻譜、瀑布圖、頻段功率趨勢、占用率條。
- 設定：中心頻率、取樣率、增益、FFT 點數、平均次數、更新率、裁邊、直流尖峰修正。
- 分析：基線比較（找出新出現的干擾）、最強訊號清單、空閒窗口建議、頻段監看與事件紀錄、全頻段位置條（可點擊跳頻）。
- 圖上標示頻段定義（FM、ISM、GNSS、WiFi、各電信業者 LTE／n78 等）、WiFi 頻道。
- 實作：子程序 `webmon/live_rx.py` 用 ctypes 直接呼叫 libbladeRF，經 stdout 管道串流 IQ；伺服器算 FFT，用 SSE 推送（2026-11 之前是 `bladeRF-cli` 加 FIFO）。

### 3.2 掃描結果（sweep）
- **完整**：每步啟動一次 `bladeRF-cli`，70–6000 MHz 約 15 分鐘，只有平均值。
- **快速**：單一 libbladeRF 連線逐步換頻、邊收邊算（2026-11 起不再用 `bladeRF-cli` 與 FIFO），全頻段約 **3 分半**（舊版實測 209 秒；新版只實測過 400–600 MHz 約 8 秒，全頻段耗時沒重測），步進 8 MHz 重疊平均，每點約 0.15 秒，另外記錄**峰值保持**欄。
- 檢視：縮放平移、頻段標示、兩檔比較（顯示差值）、刪除（移到 `sweep/.trash/`，可手動救回）。

### 3.3 分區動態監控（hop）
串流不中斷，用軟體在多個頻區之間輪流換頻，疊出準即時的廣域監控。
- 頻區以「名稱, 頻率」或「名稱, 起-迄」逐行定義（範圍會自動切成多區；`#` 開頭為註解），上限 **32 區**（伺服器回報，網頁會讀取）。
- 呈現方式可切換：**合併圖表**（所有頻區連成一張，標示各段名稱與頻率範圍，段與段之間用虛線隔開表示未監看）／**分區卡片**。
- 穩定顯示：平滑、固定縮放範圍（開始後校正一次）、固定約 8 fps 重畫、排行榜每 2 秒更新。
- 事件紀錄：各區功率超過自己的慢速基線一定 dB 時，寫入 `events.csv`。
- **響應校正**：見 3.4。
- 預設組合：無人機常用、2.4G+5.2G+5.8G、ISM 低頻、GNSS 全星系、FM、Skydio X10D、Skydio X10 控制器無線電干擾（NTO SOSB-24-V：136-174／403-512／763-776／851-870 MHz）、中華電信／遠傳／台灣大哥大 4G+5G、BluSDR-90（1980–2700 MHz，26 區）。

### 3.4 響應校正（hop 的「微笑」）
接收器在取樣頻寬邊緣會有額外雜訊，頻區兩側因此翹起。實測它的**絕對電平固定**（約 -41 dBFS @ ±22 MHz，56 MSPS），幾乎不隨增益與接收頻率改變，所以：
- 在 3.7–4.9 GHz 五個安靜頻率量測，取各頻率格的**最小值**（保守，寧可少扣），在功率域扣除；中心 20% Nyquist 以內不動，到 40% 才全額。
- **不需要終端負載**，天線保持連接即可，每個取樣率建立一次（`webmon/calib/shape_<N>M.json`）。
- 預設改為 **56 MSPS、裁邊 25%**（±14 MHz）。

| 設定（相對頻區中心高多少 dB） | ±10 MHz | ±14 MHz |
|---|---|---|
| 40 MSPS、裁邊 10%，無校正（舊設定） | +2.7 | **+11.1** |
| 56 MSPS、裁邊 15%，無校正 | +0.9 | +2.9 |
| 56 MSPS、裁邊 15%，有校正 | +0.5 | **+0.9** |

### 3.5 干擾事件
讀取 `events.csv`（時間、持續秒數、監看頻段、峰值、基線、峰值頻率），可下載。

---

## 4. 架構

```
瀏覽器 (static/index.html，純 JS + Canvas，無外部函式庫)
   │  REST (JSON)            SSE：/api/stream（live）  /api/hopstream（hop）
   ▼
webmon/server.py   ThreadingHTTPServer + Monitor 狀態機（鎖 ctl 串行化啟停）
   │
   ├─ live ──► webmon/live_rx.py（子程序，ctypes）──stdout 管道──► server 內 numpy 算 FFT
   ├─ sweep ─► sweep/bladerf_sweep.py（子程序）──► sweep/*.csv / .png
   │             ├─ 完整：每步一個 bladeRF-cli
   │             └─ --fast：單一 libbladeRF 連線（common/bladerf_io.py）逐步換頻，邊收邊算
   ├─ hop ───► webmon/hopper.py（子程序，ctypes 直接呼叫 libbladeRF）──stdout JSON 行──► server
   └─ calib ─► hopper.py --calibrate ──► webmon/calib/shape_<N>M.json
```

### 檔案

| 路徑 | 說明 |
|---|---|
| `webmon/server.py` | 網頁伺服器、模式狀態機、API、事件偵測、子程序管理 |
| `webmon/hopper.py` | 跳頻接收與校正（ctypes → libbladeRF） |
| `webmon/live_rx.py` | 即時監控的接收子程序，原始 IQ 輸出到 stdout |
| `common/bladerf_io.py` | 共用的 libbladeRF ctypes 封裝（各平台找函式庫、`BLADERF_LIB` 可覆寫）與「stdin 關閉即停止」機制 |
| `webmon/static/index.html` | 全部前端 |
| `webmon/config.json` | 上次的 live 與 hop 設定（會自動寫入） |
| `webmon/events.csv` | 干擾事件紀錄 |
| `webmon/calib/` | 響應校正檔 |
| `sweep/bladerf_sweep.py` | 掃描（完整／快速） |
| `sweep/*.csv, *.png` | 掃描結果；`sweep/.trash/` 為網頁刪除的檔案 |
| `sdrpp-src/` | SDR++ 原始碼與編譯結果（`build/`、`SDR++.app`） |

### API

| 方法 | 路徑 | 用途 |
|---|---|---|
| GET | `/api/status` | 模式、設定、裝置資訊、掃描進度、hop 狀態與校正清單、上限 |
| GET | `/api/stream`、`/api/hopstream` | SSE：live／hop 資料 |
| GET | `/api/sweeps`、`/api/sweep`、`/api/compare` | 掃描清單、縮放後的資料、兩檔差值 |
| GET | `/api/events`、`/api/events.csv` | 事件 |
| POST | `/api/config`、`/api/live` | live 設定與啟停 |
| POST | `/api/hop/start`、`/api/hop/stop`、`/api/hop/calibrate` | 跳頻啟停、建立校正 |
| POST | `/api/sweep/start`、`/api/sweep/delete` | 掃描、刪除（移到垃圾桶） |
| POST | `/api/refresh_device` | 待機時重新偵測裝置 |

### 資料格式
- 掃描 CSV：`freq_mhz,power_dbfs[,peak_dbfs]`（`peak_dbfs` 只有快速模式有）。
- `events.csv`：`time,duration_s,watch_mhz,bw_mhz,peak_db,baseline_db,peak_freq_mhz`。
- 校正檔 JSON：取樣率、頻寬、量測頻率與底噪、各偏移的額外雜訊、`excess`（2048 格的線性功率）。

---

## 5. 效能（實測）

| 項目 | 數值 |
|---|---|
| 完整掃描 70–6000 MHz | 約 15 分鐘（495 步） |
| 快速掃描 70–6000 MHz | 209 秒（742 步），曲線抖動 0.08 dB（完整版 0.12 dB） |
| 換頻（libbladeRF，host 調諧模式） | 中位數約 74 ms |
| 換頻（FPGA 調諧模式） | 約 6–16 ms（頻差小時較快） |
| 分區監控，13 區（56 MSPS，每區 10 ms） | 一圈約 209 ms（4.8 次/秒），無丟幀 |
| 分區監控，26 區（BluSDR-90） | 一圈約 469 ms（2.1 次/秒），無丟幀 |

每區觀察佔比＝每次停留時間 ÷ 一圈時間（13 區約 5%，26 區約 2%），**比一圈還短的突發訊號會漏掉**，峰值保持只能補一部分。

---

## 6. 重要經驗（避免重蹈）

- **FPGA 調諧模式必須先設定、再設定增益**，否則切換模式會把增益重設，底噪整個抬高約 25 dB。
- 串流進行中切換調諧模式或增益模式，會讓串流卡住；要先停止再設定。
- 專案路徑含空格，SDR++ 的 CMake 複製步驟與打包腳本會出錯（已用手動複製與相對路徑繞過）。
- 4 GHz 處底噪在增益 20 dB 有約 7.5 dB 的台階（-60 → -52.6 dBFS）。2026-10-28 實測 3900/4100 MHz 在增益 0/20/40/55 dB 的底噪：台階只在 20 dB 附近出現，增益 0 時消失、40 以上反向，換地點也不變，因此不是環境或硬體損壞，而是隨增益改變的（推測為 AD9361 分段增益表）。不要直接比較 4 GHz 上下的 dBFS 絕對電平。
- 分區監控區數上限 200；預設集「全頻段 70-6000 MHz」選取時會自動設 56 MSPS、裁邊 15 %（152 區），實測一圈約 2.8 秒、無掉包。該預設在相對底噪模式下，1.3–4 GHz 看起來比 4 GHz 以上高約 5–10 dB 並呈鋸齒狀，推測是邊緣雜訊校正在 4 GHz 以下不足（校正取各頻率最小包絡，而 4 GHz 以下底噪較低、邊緣相對更突出），尚未驗證。
- 「掃描結果」頁有「相對底噪（dB）」開關：用掃描檔平均線的局部 10 百分位（±27 MHz、5 MHz 格）當底噪並扣除，消除這類台階。這是相對量，不是 dBm；底噪會被大範圍連續訊號墊高。分區動態監控也有同名開關：每個頻區各自扣掉自己的底噪（hopper 回報的每區 floor），合併圖、卡片、瀑布圖與提示框都改為高於本區底噪的 dB。即時監控也有同名開關：整張圖與瀑布圖扣掉本頻段（單一 frame）的底噪，Y 軸改為高於底噪的 dB。三處都只是相對量，不是 dBm（沒有訊號源，未做 dBm 校正）。
- 多個請求同時重啟串流曾造成殘留的 `bladeRF-cli` 佔住裝置；現在以鎖串行化，並追蹤所有子程序，開始前先清掉。
- `.hidden` 的 CSS 要加 `!important`，否則會被後面的 `display:grid` 蓋過。
- WarDragon 專案（alphafox02/WarDragon）只是文件中心，BladeRF 只用在**閉源**的 DragonSig 上，無法自行編譯，所以沒有部署它，改用本專案自建的工具。

---

## 7. 已知限制與尚未驗證

- **電平是 dBFS**，沒有 dBm 校正（需要已知電平的信號源）。
- 響應校正只在**增益 20 dB** 與安靜頻區驗證過；其他增益下額外雜訊略有不同，建議換增益後重新建立。最外側仍有殘留，所以預設裁到 ±14 MHz。40 MSPS 校正後在 ±14 MHz 仍有約 +6.6 dB，不建議使用。
- **分區監控的事件紀錄與紅色警示**寫了但**沒有用真實干擾觸發過**；各預設組合（Skydio、各電信業者、GNSS、BluSDR-90）的**實際跳頻畫面沒有逐一檢視**。
- 預設集的資料來源：
  - Skydio X10D：使用者提供，**假設單位為 MHz**。
  - 中華電信、遠傳、台灣大哥大：5G 3.5 GHz 區段來自 2020 年標售結果的新聞；4G 只查到各頻段的頻寬，沒有各家的確切區塊，所以以**整段下行頻段**涵蓋（會同時看到其他業者）；合併與歸還可能使現況不同。遠傳的 900 MHz、各家的 700 MHz 無法確認，以註解行保留。
  - GNSS：依各星系公開的訊號規格整理，**未逐一對照官方介面文件**。GNSS 衛星訊號本身低於底噪，看不到衛星，只能看干擾或壓制造成的底噪變化。
  - BluSDR-90：只知道圖上的 1.98–2.70 GHz，不知道它的頻道規劃。
  - 28 GHz 毫米波超出硬體範圍（上限 6 GHz）。
- 天線狀態沒有驗證（BladeRF 無法量駐波比）。曾在 SDR++ 中勾選過 Bias-T。**即時監控、分區監控、校正與快速掃描**每次開始都會強制關閉 Bias-T，網頁也不提供開啟的控制；但**完整掃描**（`sweep/bladerf_sweep.py` 的每步擷取）沒有設定 Bias-T，沿用裝置當時的狀態（執行前曾確認為關閉）。若接 LNA 類模組（如 BT-200），要先確認供電方式，再另行加入有警告的 Bias-T 開關，並讓完整掃描也明確關閉它。
- 伺服器無驗證、僅 localhost。事件列表在伺服器重啟時會清空（`events.csv` 保留）。
- 專案資料夾內有 `DragonOS_Pi64_Beta42.img.gz`（約 4.9 GB），**不是本專案的工作產物**，用途未記錄。

---

## 8. 後續方向（尚未開始）

優先順序依「挑頻段、分析干擾」兩個目標：

1. **長時間記錄加時間對照**：每秒存各頻區功率與占用率，做歷史熱圖，並與無人機飛行紀錄對照斷線時刻。
2. **事件觸發存原始 IQ，加自適應跳頻**：事件觸發時保存前後數秒 IQ，以便判斷是 WiFi、基地台、跳頻訊號還是壓制式干擾；關鍵頻段提高輪詢頻率，可疑活動時停下來盯住。
3. **過載與假訊號辨識**：ADC 削頂警示，以及增益階梯測試（真實訊號隨增益等量變化，假訊號不會）。700–800 MHz 的基地台訊號在增益 20 dB 時已接近滿量程。
4. **dBm 校正**：需要外部參考信號源。
5. 之後再考慮：雙天線測向（兩個接收通道共用同一本地振盪器，只能看同一頻率）、帶 GPS 的場勘地圖、報告輸出、在 Linux 小主機常駐並透過 MQTT／TAK 對外送出（接上 WarDragon／DragonSync 生態，需先加驗證）。

---

## 9. 進度紀錄

| 階段 | 內容 |
|---|---|
| 評估 | 研究 WarDragon，確認其 BladeRF 部分為閉源，決定改自建工具 |
| 硬體就緒 | 安裝 libbladeRF；下載 xA4 FPGA；韌體升級到 2.6.0；確認 USB 3.0 |
| 觀察工具 | 編譯 SDR++（含 bladeRF 模組） |
| 掃描 | 全頻段掃描腳本；修正梳狀假象（窗口縮到 ±12 MHz、遮蔽直流尖峰、重疊平均）；快速模式 |
| 網頁 | 即時頻譜、設定、掃描檢視／比較／刪除、事件紀錄、基線比較、占用率、空閒窗口建議 |
| 穩定性 | 修正殘留 `bladeRF-cli` 佔用裝置的競爭問題 |
| 分區動態監控 | 直接呼叫 libbladeRF + FPGA 調諧；合併圖表與卡片；防跳動；多個預設組合 |
| 響應校正 | 量化邊緣雜訊；建立校正；56 MSPS + 裁邊 25% 為新預設；32 區上限 |

- 2026-10-28：分區監控「停止後重啟會卡住」。重現到一次停止耗時 6 秒、hopper 被 SIGKILL（-9），之後 BladeRF 從 USB 匯流排消失（system_profiler 看不到，bladeRF-cli -p 找不到裝置）。推測是串流中途停滯（可能與 USB 供電有關，未確認）→ SIGINT 無法中斷阻塞中的 sync_rx → 被強殺。已把 SIGINT 等待改為 6 秒、hopper 的 sync_rx 逾時改為 1.5 秒；效果未驗證。

## 錄製頻譜資料（2026-10-29）

- 即時監控與分區監控各有一個「● 錄製」按鈕（手動開始/停止，錄製在伺服器端進行，重新整理網頁不會中斷；停止接收或停止分區監控時自動結束錄製）。
- 檔案在 `webmon/recordings/`，格式為 gzip 壓縮的 JSON Lines，第一行是 header（設定）。網頁的「錄製檔」可下載，「移到垃圾桶」是移到 `recordings/.trash/`，不會直接刪除。
- 即時：每秒一筆 `{"t","p","floor"}`（p＝該秒最大值，dBFS），頻率幾何（f0、df、n、rate、freq）在前面的 `geom` 行；設定改變時會再寫一行 `geom`。實測約 1 KB/秒（每小時數 MB）。
- 分區：每個頻區每一幀一筆 `{"t","z","f0","df","p","pk","floor"}`（p＝平均、pk＝峰值，dBFS），`hello` 行含頻區名稱。實測 3 個頻區約 40 KB/秒；全頻段 152 區估計每小時約 200–300 MB（估算，未實測）。
- 錄的是頻譜（dBFS，相對值），不是 IQ。自己讀檔：`gzip.open(path, "rt")` 逐行 `json.loads`。

### 回放（「回放」分頁）

- 選錄製檔後按「載入」：瀏覽器下載並解壓縮，上圖是目前時間的頻譜，下圖是整段錄製的瀑布圖（上＝開始、下＝結束，白線＝目前時間）。可播放／暫停、1–60 倍速、拖拉滑桿或點瀑布圖跳轉，游標移上去顯示頻率、時間與值；「相對底噪」開關和分區的「平均／峰值」可切換。
- 分區錄製會把各頻區依頻率接起來（虛線＝頻區間的空隙，橫軸依欄位排列，標的頻率不是線性刻度）；超過 4000 點時取每格最大值。時間以 ≥1 秒的格子重取樣（每格取當時各區最新一幀）。
- 限制：錄製中的檔案不能載入（要先停止）；整檔讀進瀏覽器記憶體，數百 MB 以上的檔案可能很慢。**即時錄製的回放已用真實檔案測過；分區錄製的回放只做了語法檢查，沒實測**（測試時 BladeRF 掉線）。

## 協作與 git

- 協作規則見 [CONTRIBUTING.md](CONTRIBUTING.md)：`main` 只透過 PR 合併、分支 `類型/說明`、Conventional Commits。
- **重建 SDR++**：`sdrpp-src/` 不在 git 裡（上游 https://github.com/AlexandreRouma/SDRPlusPlus.git，目前編譯的 commit 為 `f10fb98af8b80de01de94e60c17608e4281e2515`，含 bladeRF 模組）。需要時自行 clone 到 `sdrpp-src/` 並依上游說明編譯；專案路徑含空格會讓 CMake 複製步驟出錯（見上方已知問題）。
- 不在 git 裡的本機資料：`sweep/*.csv|png`（量測）、`webmon/recordings/`、`webmon/events.csv`、`webmon/config.json`、`webmon/server.log`。

## 跨平台重構（分支 `refactor/cross-platform-core`，2026-11）

- 目的：讓共用層不依賴 POSIX 專有功能（FIFO、`select` 管道、`SIGINT`/`SIGKILL`），以便之後在 Windows 上驗證。
- 做法：即時監控與快速掃描改用 `common/bladerf_io.py`（ctypes 直接呼叫 libbladeRF）；子程序改以「關閉 stdin」要求乾淨結束，再 `terminate`、`kill`；子程序用 `sys.executable`，不再寫死 `.venv/bin/python`；函式庫依平台尋找（macOS dylib、Linux `.so`、Windows `bladeRF.dll`）。
- **Mac 上實測**：即時 4 次、分區 4 次的啟動→停止循環、即時→分區→即時的切換，全部正常；停止耗時約 0.02–0.09 秒，結束後沒有殘留子程序；快速掃描 400–600 MHz 的底噪曲線形狀與舊版一致（突發訊號因時間不同而不同）。
- **尚未驗證**：Windows（驅動、DLL 路徑、USB 直通）、Linux；完整掃描（非 `--fast`）仍用 `bladeRF-cli` 逐步擷取到檔案，沒改；全頻段快速掃描的耗時；長時間穩定性（之前的掉線與停止後重啟卡住是否改善，只測了短循環）。
- 已知遺留：伺服器本身被結束時，仍可能留下子程序（舊版是 `bladeRF-cli`，現在是 `live_rx.py`/`hopper.py`）；尚未處理。
