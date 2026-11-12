# Deli assign dashboard

在本機執行的派工日曆。使用 Python 3.12 標準函式庫與 SQLite，不需要安裝額外套件。

## 啟動

```sh
cd ~/playground/deli-assign-dashboard
python3 server.py --port 8765
```

開啟 http://127.0.0.1:8765 。服務僅監聽本機，不對區域網路開放。前端需透過此服務開啟，直接開啟 HTML 檔無法登入或取得資料。

預設本機測試帳號如下。

| 帳號 | 密碼 | 權限 |
| --- | --- | --- |
| admin | admin123! | 查看工單、新增與修改派工 |
| user | user123! | 查看日曆與工單詳情 |

若要設定其他初始密碼，可在第一次啟動前設定 `DELI_ADMIN_PASSWORD` 與 `DELI_USER_PASSWORD`。既有帳號不會因環境變數或重啟而被覆寫。本版沒有帳號管理或變更密碼介面。

## 日曆與派工

- 週曆與月曆的日期方向一致，週一到週日由左至右。週曆以北部 A、南部 A、南部 B 為三個團隊列。
- 每組每天至少保留 5 張工單的高度。超過 5 件會繼續延展，沒有硬性限制為 5 件。工單依出勤時間排序。
- 週曆維持可閱讀的欄寬。窄視窗在日曆區左右捲動，團隊欄固定在左側，不把七天縮成窄欄或改成直排。
- 點選待排或已派工工單可查看詳情。admin 可指定團隊、日期與起訖時間，儲存至後端；user 僅顯示詳情。
- 後端檢查日期、時間、工單與團隊，以及同團隊同時段衝突。相鄰時段可以安排，不含跨日派工或交通時間規則。
- 月曆點選日期可切回該週；工單過多時提供其餘筆數。團隊與狀態篩選共用於兩種視圖。

## 資料與身份

`data/deli.sqlite3` 儲存帳號、sessions、團隊、工單與派工。初始化時由 `seed.json` 載入目前台灣日期所在週的示意資料，包含同一天同一組 5 件工單。後續啟動不會重新匯入或覆蓋資料。畫面資料仍為示意資料，不含正式派工單來源。

密碼採隨機 salt 與 PBKDF2-SHA256 雜湊。登入後使用 12 小時的 HttpOnly、SameSite=Strict cookie，伺服器僅儲存 session token 的雜湊。角色由後端帳號資料決定；user 直接呼叫寫入 API 也會被拒絕。登出會撤銷 session。寫入請求驗證 Origin，服務預設僅接受 localhost 或 127.0.0.1 的 Host，公開模式額外接受明確設定的 HTTPS 主機名稱。

本版供本機測試使用，使用 HTTP 與預設測試帳號。尚未提供正式部署、HTTPS、外部身份供應商、工單建立表單或跨日派工。

## 程式分工

- `index.html`：登入、週月曆、工單詳情與派工編輯。沿用 shadcn 風格的中性色彩 token，尚未安裝正式 shadcn/ui 套件。
- `server.py`：HTTP API、身份與權限、SQLite 持久化、排程驗證。
- `seed.json`：首次啟動的示意資料。
- `tests/test_server.py`：實際 HTTP 請求的登入、權限、持久化、衝突與驗證測試。
- `tests/verify-browser.cjs`：以獨立暫存資料庫啟動服務，執行真實瀏覽器測試，不改動本機使用資料。

資料模型分為 `Team`、`WorkOrder`、`Assignment`、`User` 與 `Session`。工單與派工紀錄分開；目前每張工單最多一筆派工，資料庫以 UNIQUE 約束避免重複。排程日期與時間採台灣當地時間，不以瀏覽器 UTC 換日。

## 驗證

```sh
python3 -m unittest discover -s tests -v
```

瀏覽器測試另需可用的 Playwright 與 Chrome。透過環境變數指定已安裝模組或執行檔，也可使用 Node 預設模組解析。

```sh
PLAYWRIGHT_MODULE=/absolute/path/to/playwright CHROME_PATH=/usr/bin/google-chrome node tests/verify-browser.cjs
```

瀏覽器測試涵蓋 1440、728、390px，淺色與深色，週曆、月曆與編輯器共 18 種組合。另驗證登入錯誤、admin 儲存、user 唯讀與 API 拒絕、重新整理及後端重啟後資料保留、時間衝突。截圖及結果寫入 `.verification/`。

## 打包基底

```sh
python3 scripts/package.py
```

`dist/` 會產生 ZIP 及 SHA-256 校驗檔。ZIP 包含程式、種子資料、測試與檔案雜湊 manifest，不包含目前資料庫、session、暫時入口的密碼或執行檔。解壓縮後用 `python3 server.py` 啟動，首次啟動會建立示意資料與本機測試帳號。

## 暫時外部測試

可透過 Cloudflare Quick Tunnel 將本機服務接到暫時 HTTPS 網址。這次外部測試使用 `.runtime/preview.sqlite3` 的獨立資料副本，另產生 admin 和 user 密碼，並清除既有 session。外部測試的編輯不會回寫 `data/deli.sqlite3`。

伺服器的 `--public-origin https://完整主機名稱` 僅允許該 HTTPS 來源，並對公開請求使用 Secure cookie。公開模式會隱藏本機測試帳號提示，也會拒絕使用預設帳密啟動。建立新公開測試資料庫時，應先用環境變數設定新的帳密，再以公開模式啟動。

此次 tunnel 與外部測試後端最多執行 8 小時。主機關機、休眠、斷網或任一程序停止，都可能提早中斷；重新建立 tunnel 時網址會改變。關閉外部測試不影響原本的 8765 本機服務。

[Cloudflare Quick Tunnel 文件](https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/)
