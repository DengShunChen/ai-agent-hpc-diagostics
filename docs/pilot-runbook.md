# 試點操作手冊

給據點維運工程師與試點負責人使用。說明如何在單一據點進行試點，並依
[business-impact.md](business-impact.md) 的量測方案，取得「故障定位時間」導入前後的對照數據。

> **原則：AI 提案，工具提供證據，由人決定。**
> 平台只讀取人工匯出的資料，不連線任何正式系統，也不執行任何處置。
> 試點期間，正常的處理程序與權責都不變：平台的結果僅供參考，所有處置仍依據點既有 SOP 由人執行。
> 試點結束、依本手冊完成分析前，**不得對內或對外宣稱任何時間縮短的幅度**。

## 1. 角色

| 角色 | 職責 |
|---|---|
| 試點負責人 | 安排時程、通過安全審查、每週檢查、最後分析與報告 |
| 據點工程師（審查者） | 匯出資料、在審查頁面上核准、退回或要求修改；在工單上記錄時間戳記 |
| 資安窗口 | 審查資料分類、匯出程序、主機位置與 token 發放對象 |
| 標註者 | 由據點工程師擔任，負責標註評估案例的正確答案（不可由平台開發者擔任） |

## 2. 時程

| 階段 | 期間 | 完成條件 |
|---|---|---|
| 0. 準備 | 約 2 週 | 安全審查通過；據點設定檔、金鑰、token、評估案例都已就緒 |
| 1. 基準期 | 4 到 8 週 | 不使用平台，照常處理事件並記錄時間戳記 |
| 2. 輔助期 | 與基準期等長 | 由同一團隊處理同類事件，並使用平台 |
| 3. 分析 | 約 1 週 | 執行 `compare-pilot`，依第 8 節撰寫報告 |

基準期與輔助期應盡量等長並且相鄰，以降低季節性與人力變動的影響。

## 3. 準備

以下指令都在 repo 目錄下執行。先定義一個簡寫：

```bash
cd ~/EACZA/hpc-ai-agent
hpc() { PYTHONPATH=src .venv/bin/python -m hpc_agent.main "$@"; }
```

### 3.1 安全審查（必要，未通過不得開始）

- 資料分類：哪些匯出欄位可以離開來源系統。
- 匯出程序：由誰、用哪個據點核准的工具匯出，存放在哪裡。
- 主機位置：試點主機只綁定本機位址；若要給其他人使用，必須放在有驗證的 TLS 反向代理後面。
- token 發放對象，以及 `audit-data/` 的存放與保留期限。

### 3.2 試點專用目錄（放在 repo 外面）

```bash
mkdir -p ~/hpc-pilot/{exports,data,audit-data}
chmod 700 ~/hpc-pilot
```

| 目錄 | 內容 |
|---|---|
| `~/hpc-pilot/exports/` | 每次匯出的 CSV，每起事件一個子目錄 |
| `~/hpc-pilot/data/` | 匯入後的資料（`fixtures/`、`test_data/`、`knowledge/`） |
| `~/hpc-pilot/audit-data/` | 試點期間的診斷與核准紀錄，與 POC 的紀錄分開 |

### 3.3 據點設定檔

```bash
cp config/sites/default.yaml config/sites/<site-id>.yaml
```

依據點工程師的共識，調整儲存容量門檻、OST 不均衡、GPU 溫度上限、時間關聯視窗與處置信心門檻。
設定檔只能調整診斷門檻，不能放寬任何安全規則。修改後需要重建映像，容器才讀得到。

### 3.4 假名化金鑰

```bash
read -rs HPC_AGENT_PSEUDONYM_KEY && export HPC_AGENT_PSEUDONYM_KEY   # 至少 32 個字元
```

- 金鑰由據點保管（例如密碼管理器），**整個試點期間使用同一把**，否則同一台主機會得到不同的假名。
- 不要寫進檔案、腳本、shell 歷史或 repo。

### 3.5 知識庫

把據點去識別化後的 SOP（`SOP-*`）、手冊（`MAN-*`）與過去事件，以 Markdown 放進
`~/hpc-pilot/data/knowledge/{sop,manuals,incidents}/`。若暫時沒有，也要建立空目錄：

```bash
mkdir -p ~/hpc-pilot/data/knowledge/{sop,manuals,incidents}
```

### 3.6 審查者 token

每位審查者一個 token，名稱用 `ops.<名字>`：

```bash
hpc create-operator-token ops.<名字> --tokens-file ~/.config/hpc-agent/operators.json
```

token 只會顯示一次，請本人自行保存，不要貼到聊天、工單或 Email。

### 3.7 評估案例（上線前門檻）

由據點工程師挑選 10 到 20 起過去的事件，標註正確的領域與根本原因，放在 repo 外的目錄
（格式參考 `evaluation/cases/`），然後執行：

```bash
hpc evaluate --cases ~/hpc-pilot/cases --output ~/hpc-pilot/evaluation.md
```

開始輔助期之前，領域分類、拒絕下結論的正確率，以及無根據主張率都必須達到事先約定的門檻。

> 注意：報告開頭的說明文字是為 POC 的合成案例寫的（「案例為合成資料、由開發者標註」）。
> 用據點的真實案例時，請看各項指標與逐案結果，並在報告中自行註明案例來源與標註者。

## 4. 時間戳記的定義（兩個時期必須完全一致）

試點的主要成效指標是**故障定位時間 = `localized_at` − `detected_at`**。定義一旦決定，整個試點都不能更改。

| 欄位 | 定義 |
|---|---|
| `detected_at` | 告警產生或使用者回報的時間，以工單上最早的時間為準 |
| `localized_at` | 工程師在工單上寫下「根本原因所在的元件」（例如某個 OST、某張 GPU、某個連接埠）的時間 |
| `resolved_at` | 服務恢復的時間（選填，用來計算 MTTR） |

- 時間一律使用 ISO 8601 格式並附時區，例如 `2026-11-02T09:15:00+09:00`。
- `localized_at` 必須是工程師自己確認的時間，**不是**平台產生診斷的時間。
- 事件類別只能是：`storage`、`scheduler`、`network`、`gpu`、`cross-domain`。

## 5. 基準期

- 照常處理事件，不使用平台。
- 每起事件都依第 4 節記錄三個時間與類別。
- 同時保留匯出資料（第 6.1 節），之後可以當作評估案例，但**基準期的事件不要拿給平台診斷**。

## 6. 輔助期：每起事件的處理流程

### 6.1 匯出

依安全審查核准的程序，把這起事件的資料匯出成 CSV，放進 `~/hpc-pilot/exports/<事件編號>/`。
格式見 `src/hpc_agent/ingest/csv_export.py`，範例見 `examples/site-export/`。缺少的資料留空即可，平台會把它列為「缺少的資訊」，不會猜測。

### 6.2 匯入

```bash
hpc import-export ~/hpc-pilot/exports/<事件編號> --output ~/hpc-pilot/data --dry-run
hpc import-export ~/hpc-pilot/exports/<事件編號> --output ~/hpc-pilot/data
```

- 若事件被拒收，報告會寫出原因與欄位位置，但不會顯示內容。請修正匯出內容後重新匯出，**不要手動修改匯入後的檔案**。
- 匯入完成後，**請人工檢查自由文字**（症狀、告警、日誌、備註）。平台只會替換已知的主機、作業與檔案系統名稱，其他個人或客戶資訊無法自動判斷。
- 同一起事件不能重複匯入。若要修正，先刪除 `~/hpc-pilot/data` 底下該事件的檔案，再重新匯入。

### 6.3 啟動平台（試點期間持續運作）

```bash
HPC_AGENT_DATA_DIR=~/hpc-pilot/data HPC_AGENT_SITE=<site-id> \
HPC_AGENT_STATE_DIR=~/hpc-pilot/audit-data HPC_AGENT_PORT=8088 \
HPC_AGENT_OPERATOR_TOKENS=~/.config/hpc-agent/operators.json \
MODEL_PROVIDER=ollama MODEL_NAME=qwen2.5:3b \
scripts/podman_run.sh -d
```

- 新匯入的事件在重新整理頁面後就會出現，不需要重啟容器。
- 啟動前會檢查資料目錄有沒有 `fixtures/`、`test_data/`、`knowledge/`，以及據點設定檔是否存在，缺少就不會啟動。
- 不使用本機 LLM 時，拿掉 `MODEL_PROVIDER` 與 `MODEL_NAME` 這兩個設定即可。

### 6.4 診斷與審查

1. 打開 http://127.0.0.1:8088 ，貼上自己的 token。
2. 選擇事件並執行診斷。
3. 閱讀審查頁：事實、假設、已排除的原因、缺少的資訊、處置計畫與暫緩原因。
4. 依據點 SOP 自行查證，再按下 **核准**、**退回** 或 **要求修改**，並寫下理由。
   - 核准只代表「同意這份提案」，平台**不會執行任何動作**。
   - 若平台的結論有錯，請退回並寫明原因，這是試點最重要的回饋。
5. 在工單上記錄平台的 **run ID**，之後才能把工單和平台紀錄對應起來。

### 6.5 處置

處置一律依據點 SOP 由人執行，並照第 4 節記錄 `localized_at` 與 `resolved_at`。

## 7. 每週檢查（試點負責人）

```bash
export HPC_AGENT_STATE_DIR=~/hpc-pilot/audit-data
hpc verify-audit     # 稽核紀錄的雜湊鏈必須完整
hpc metrics          # 診斷耗時、審查耗時、推翻率（依據點分組）
```

| 檢查項目 | 出現以下情況時 |
|---|---|
| `verify-audit` 失敗 | **立即停止試點**，保留現場並通知資安窗口 |
| 推翻率（退回與要求修改的比例）連續兩週上升 | 檢討失敗案例，必要時暫停輔助期 |
| 審查時發現無根據的結論 | 記錄案例，並加入評估案例 |
| 懷疑有未去識別化的資料進入平台 | **立即停止**，依資安程序處理，並刪除受影響的匯入檔案 |

`hpc metrics` 顯示的是平台的流程耗時，**不是 MTTR**，不能拿來當作成效。

## 8. 分析與報告

### 8.1 匯出工單資料

依 `evaluation/pilot/pilot-template.csv` 的欄位，每起事件一列，`period` 填 `baseline` 或 `assisted`：

```
incident_id,site,category,period,detected_at,localized_at,resolved_at
```

檔案放在 `~/hpc-pilot/`，不要放進 repo。

### 8.2 執行比較

```bash
hpc compare-pilot ~/hpc-pilot/pilot.csv
```

每個類別（以及全部合計）會列出兩個時期的中位數、差值與 95% 信賴區間，並給出以下其中一個判讀結果：

| 判讀結果 | 意義 |
|---|---|
| `faster with assistance` | 信賴區間整段小於 0：輔助期較快 |
| `slower with assistance` | 信賴區間整段大於 0：輔助期較慢 |
| `no significant difference` | 信賴區間包含 0：**無法證明有差異** |
| `insufficient sample` | 任一時期少於 10 起事件，不做判讀 |

格式錯誤或時間倒退的資料列會被略過並列出，不會自動修正。請回頭修正工單資料。

### 8.3 報告必須包含

- 據點、兩個時期的起訖日期、各類別的事件數。
- 每個類別的中位數、差值與信賴區間，以及判讀結果原文。
- 推翻率、無根據主張率、`verify-audit` 結果。
- 已知的干擾因素：事件類型組成、人力、季節性與系統變更。這是觀察性的前後比較，不是對照實驗。
- 只有判讀結果為 `faster with assistance` 的類別，才可以寫「該類別的定位時間在本次試點中縮短」，並且必須同時附上期間、據點、樣本數與信賴區間。

### 8.4 不可以這樣寫

- 「平台將 MTTR 縮短了 X%」，卻沒有附上期間、據點、樣本數與信賴區間。
- 把 `no significant difference` 或 `insufficient sample` 寫成「有改善趨勢」。
- 把不同據點、不同設定檔的資料合併計算卻沒有說明。
- 把 `hpc metrics` 的流程耗時當作 MTTR。

## 9. 試點結束

```bash
podman stop hpc-ai-agent
```

- 依安全審查決定的保留期限，處理 `~/hpc-pilot/` 底下的匯出、匯入資料與稽核紀錄。
- 刪除或撤銷試點用的 token（從 `operators.json` 移除對應的那一行）。
- 依分析結果更新 [aiops-proposal.md](aiops-proposal.md) 的「實證狀態」一節。
