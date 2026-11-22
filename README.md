# 典当质押赎回采证台

离线的质押担保数据采录台。权属估价决定放款上限；库房封签和独立盘点决定担保物是否完整；还款、展期与通知决定赎回或绝当资格。三本账各有 CLI、持久实体、状态变化和失败样例。所有人物与物品都是合成数据。

```powershell
python -m pip install . --no-deps --no-build-isolation
python -m pawnproof demo --db demo.sqlite
python -m pawnproof audit --db demo.sqlite
python -m pytest -q
```

单独入口：`asset-import`、`ownership-import`、`appraisal-import`、`vault-import`、`custody-import`、`loan-import`、`payment-import`、`extension-import`、`notice-import`、`decision-import`，均用 `file --db state.sqlite`。正常 fixture 在 `fixtures/*-ok.json`，权属错配、封签破损、无通知绝当的反例在 `fixtures/*-bad.json`。每份来源文件原字节与 SHA256 入库，`audit` 在空库重放并比较全部业务状态。

首版仅模拟本地签署身份和物品凭据；不接入真实鉴定、支付、保险或法定通知渠道。金额整数分，输入须为正且精确到分。应用程序只给出本地业务决定，不构成真实金融或法律意见。

## 隐私处置作业（PR1 获批之后）

`privacy-request`/`privacy-review` 只决定能否撤下普通访问；真正的字段级处置由 `privacy-plan`、`privacy-execute`、`privacy-verify` 完成。执行前必须锁定路线与保留目的，之后不可倒选：

- `controlled_retention`：身份原文封入双人 XOR 拆分授权的离线保管包；柜台/普通报告只见不可逆代号 `PID-*`；监管在两份保管份额同时到场时可复核原始 owner，并在时间线留痕。
- `minimal_retention`：销毁解密材料，仅保留金额、时刻、同一客户关系的 HMAC 不可逆承诺与验证数据；监管能验证 500.00+25.00 与已付 525.00 清账，但姓名不可恢复。

字段清单分三类：`active_pledge_required`（进行中质押必需，阻断处置）、`historical_proof`（金额/时间/关系，留值去身份）、`no_retention_basis`（明文凭据，必须改写或封包）。处置跨主 SQLite、离线备份批次与对外卷宗清单恢复：每份受控副本必须被刷新或持有 `privacy-copy-attest` 登记的不可追回例外，否则 PR1 只能报告 `incomplete`。状态机在 `preconditions → distribution_boundary → seal_package → commitments → rewrite_main_db → disposal_state → post_verify` 各步落盘，任一步崩溃后重跑幂等；`audit` 在处置后不再给无条件 `replay_ok`，而以 `privacy_disposal` 块说明红acted 来源、承诺校验、原件可否恢复与完整重放是否可能。

`privacy-naive` 在一次性副本上演示三种常规做法的具体失败：只在报表遮盖（`source_file.raw` 与副本继续明文）、只 UPDATE 身份列（与原文重放不一致、重放即还原身份）、只删原文（重放链断裂且付款依据成孤儿）。

