# 系統架構（繁體中文）

**AI 提出建議，工具提供證據，由人決定。** 英文版見 [README.md](../README.md#architecture)。

```mermaid
flowchart TB
    subgraph IN["1 · 離線資料匯入"]
        SIM["hpc-agent simulate<br/>模擬叢集、合成資料<br/>sim- 名稱、注入預設根因"]
        EXP["據點匯出檔<br/>CSV、原始 log、指令輸出"]
        IMP["hpc-agent import<br/>格式合約檢查<br/>去識別化、憑證掃描"]
        SIM --> IMP
        EXP --> IMP
    end

    IMP --> ROOT[("資料根目錄（唯讀）<br/>fixtures/ · test_data/<br/>log 索引 · knowledge/")]
    ROOT --> DA["唯讀資料轉接器<br/>大小限制、憑證掃描"]
    DA --> TG["工具閘道 Tool Gateway<br/>白名單唯讀工具<br/>未列入的一律拒絕<br/>不可註冊<br/>shell、exec、ssh 類工具"]
    TG --> SG["ScopedGateway<br/>每個 Agent 只拿到<br/>config/agents/*.md<br/>列出的工具"]

    subgraph RULES["2 · 規則式 Agent"]
        T["分流 Agent（Triage）<br/>分類事件、挑選專家<br/>不使用工具"]
        SP["專家 Agent<br/>排程 PJM/Slurm<br/>儲存 FEFS/Lustre<br/>網路 · GPU<br/>可請其他領域協助查看"]
        C["RCA 協調者<br/>排序假設、排除原因<br/>整理時間軸<br/>從不確認根因"]
        T --> SP --> C
    end
    SG --> SP

    REM["處置 Agent<br/>依劇本提出處置建議<br/>附回復步驟與 SOP<br/>不使用工具，從不執行"]
    GATE{"Cascade 閘門<br/>規則的首位結論<br/>只說明作業怎麼結束？"}
    C --> GATE
    GATE -->|"否：已指出原因<br/>或證據不足"| REM
    GATE -->|"是"| LLMA

    subgraph AI["3 · LLM 第二意見"]
        LLMA["LLM 分流與專家 Agent<br/>模型只提議工具呼叫<br/>由 harness 執行"]
        CHK["事實檢查<br/>只保留有工具資料<br/>佐證的內容<br/>log 必須逐字引用"]
        MERGE["合併<br/>規則的首位結論維持第一<br/>通過檢查的 LLM 原因<br/>列在其下"]
        LLMA --> CHK --> MERGE
    end
    SG --> LLMA
    PROV["模型供應者（選用）<br/>本機 Ollama qwen2.5:7b<br/>或 Claude：只限合成<br/>或去識別化資料<br/>外送前掃描、花費上限"] -.-> LLMA
    MERGE --> REM

    REM --> GUARD["執行期檢查 verify_run<br/>每個事實都對得上<br/>一次工具呼叫"]
    GUARD --> SAVE["存檔，等待人工核准<br/>AWAITING_HUMAN_APPROVAL"]

    subgraph HUMAN["4 · 由人決定"]
        UI["審查頁面、HTTP API<br/>或 CLI<br/>以 bearer token 識別操作者"]
        H{"操作者決定"}
        UI --> H
    end
    SAVE --> UI
    H -->|"核准 APPROVED"| DONE["只記錄決定<br/>不執行任何動作"]
    H -->|"退回 / 要求修改"| REV["重新診斷<br/>成為新的一次執行"]

    AUD[("audit-data/<br/>hash 串鏈稽核紀錄<br/>與執行追蹤")]
    SAVE -.-> AUD
    H -.-> AUD
```

## 說明

- **資料只能從唯讀路徑進來。** Agent 讀資料只能經過唯讀資料轉接器和白名單工具閘道，每個 Agent 的
  ScopedGateway 再把範圍縮小到它自己的工具。系統不連線到叢集，也不執行任何指令。
- **規則先答。** 規則式 Agent 以 Microsoft Agent Framework 工作流程執行：分流 Agent 挑選專家，專家用唯讀工具
  收集證據，RCA 協調者排序假設，但從不確認根因。
- **LLM 只在規則說不出原因時才補充。** 規則的首位結論如果只說明作業怎麼結束（例如逾時、被終止、非零結束碼），
  cascade 閘門才讓 LLM Agent 分析。模型只能提議工具呼叫，提出的原因必須通過事實檢查，log 必須逐字引用。
  合併時規則的答案維持第一，LLM 的原因列在下方作為第二意見。規則因證據不足而不下結論時，LLM 不會介入。
- **處置只是建議。** 處置 Agent 沒有工具，只依劇本提出建議；`verify_run` 檢查每個事實都對得上工具呼叫後才存檔。
- **由人決定。** 操作者在審查頁面、HTTP API 或 CLI 做決定；核准只記錄決定，不執行任何動作。每一步都寫進
  hash 串鏈的稽核紀錄。

`hpc-agent diagnose` 和審查頁面執行規則式 Agent，或以 `HPC_AGENT_REASONING=llm` 改由 LLM Agent 執行；cascade
（閘門、LLM 第二意見、合併）用於 `hpc-agent evaluate --reasoning cascade` 和
`hpc-agent drill-prepare --reasoning cascade`。圖中虛線表示設定或紀錄，不是資料流向。
