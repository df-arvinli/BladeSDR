# 協作方式

## 環境
```bash
cd sweep && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt   # 掃描與網頁伺服器共用
cd ../webmon && ../sweep/.venv/bin/python server.py --port 8080                   # http://127.0.0.1:8080
```
BladeRF 同一時間只能被一個程式使用；沒接裝置時網頁仍可開（回放、掃描結果、事件）。

## 分支
- `main`：永遠可以執行，只透過 Pull Request 合併，不直接推。
- 開發分支從最新的 `main` 切出，命名 `類型/簡短說明`：`feat/`、`fix/`、`docs/`、`chore/`、`refactor/`（例：`feat/replay-peaks`、`fix/hop-stop-hang`）。
- 一個分支只做一件事，生命週期以天計；合併後刪除分支。

## Commit
- 訊息用 Conventional Commits：`feat: 回放加入峰值標記`、`fix: 分區停止後重啟卡住`、`docs: …`。
- 第一行說「做了什麼」，內文寫「為什麼」與已知限制；沒驗證的部分要寫出來。
- 不要提交：量測資料（`sweep/*.csv|png`）、錄製檔、`webmon/config.json`、`events.csv`、`.venv/`、`sdrpp-src/`、任何金鑰。（`.gitignore` 已擋）

## Pull Request
1. `git switch -c feat/xxx`，小步 commit，`git push -u origin feat/xxx`。
2. 開 PR 到 `main`，照範本填寫，至少一位同事 review 後再合併（建議 Squash and merge）。
3. 合併前 `git fetch && git rebase origin/main`（或 merge main）並解決衝突。
4. 有裝置才能驗證的變更，PR 裡寫清楚是否實測過。

## 文件
改功能就同步更新 README；已知問題與未驗證事項寫在 README 的「已知問題」。
