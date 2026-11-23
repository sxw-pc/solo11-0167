# 典当质押赎回采证台

离线的质押担保数据采录台。权属估价决定放款上限；库房封签和独立盘点决定担保物是否完整；还款、展期与通知决定赎回或绝当资格。三本账各有 CLI、持久实体、状态变化和失败样例。所有人物与物品都是合成数据。

```powershell
python -m pip install . --no-deps --no-build-isolation
python -m pawnproof demo --db demo.sqlite
python -m pawnproof audit --db demo.sqlite
python -m pytest -q
```

单独入口：`asset-import`、`ownership-import`、`appraisal-import`、`vault-import`、`custody-import`、`loan-import`、`payment-import`、`extension-import`、`notice-import`、`decision-import`，均用 `file --db state.sqlite`。正常 fixture 在 `fixtures/*-ok.json`，权属错配、封签破损、无通知绝当的反例在 `fixtures/*-bad.json`。每份来源文件原字节与 SHA256 入库，`audit` 在空库重放并比较全部业务状态。

## 版本化卷宗出库与复核

监管签收后的卷宗只代表签收时点，不再自动等于当前质押状态。增强工作流（与原 `dossier-prepare`/`dossier-review` 并存、互不影响）把每份卷宗做成不可变的时点封存件：

- `dossier-capture`：写入采录水位（已导入源文件序列及其重算摘要）、全部源文件身份、贷款/质物状态时点、公开字段清单与撤回链接；每次出库自动把上一版的当前资格撤销，旧文件、旧摘要、旧签收事实全部保留，不覆盖文件也不复用 digest。
- `dossier-release`：按事前授权的审查目的（`historical_transaction_review` 或 `current_custody_review`）生成选择性披露包。客户敏感字段（customer、owner、claimant、serial 等）以加盐承诺保留在签名范围内，公开材料只含其摘要，验证方可以用承诺确认“所见字段与同一封存原件一致”，却无法还原敏感值；同一卷宗的多份披露包共享同一根承诺，可证同源同版。
- `dossier-verify`：仅凭公开材料重算叶子摘要、根承诺与披露包承诺，并交叉核对 SQLite 当前指针、封存文件与撤回记录。
- `dossier-acknowledge`：登记外部回执；authority 与 digest 同时匹配才记为 matched，回执历史只追加。
- `dossier-adjudicate`：两条审查路径在出库前锁定目的与可见字段，发现贷款赎回后不得改标。历史核查可对赎回前的 active/sealed 快照给出 `historical_archive_pass` 并强制标注“非当前在押证明”；当前在押核查要求最新水位，对旧版给 `current_claim_stale_version`，对新版按真实状态给 `current_custody_pass` 或 `current_pledge_released`；证据缺失或冲突给 `evidence_missing_or_conflict`，全部原因可重算。
- `dossier-recover` / `dossier-audit` / `dossier-report`：发布文件、SQLite 指针与外部回执的对账恢复（缺失即从注册表重建、冲突只报告不覆盖、孤立文件单列），以及“历史归档可验 / 当前监管清晰 / 证据缺失或冲突”三桶审计。

首版仅模拟本地签署身份和物品凭据；不接入真实鉴定、支付、保险或法定通知渠道。金额整数分，输入须为正且精确到分。应用程序只给出本地业务决定，不构成真实金融或法律意见。
