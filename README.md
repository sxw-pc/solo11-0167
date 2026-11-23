# 典当质押赎回采证台

离线的质押担保数据采录台。权属估价决定放款上限；库房封签和独立盘点决定担保物是否完整；还款、展期与通知决定赎回或绝当资格。三本账各有 CLI、持久实体、状态变化和失败样例。所有人物与物品都是合成数据。

```powershell
python -m pip install . --no-deps --no-build-isolation
python -m pawnproof demo --db demo.sqlite
python -m pawnproof audit --db demo.sqlite
python -m pytest -q
```

单独入口：`asset-import`、`ownership-import`、`appraisal-import`、`vault-import`、`custody-import`、`loan-import`、`payment-import`、`extension-import`、`notice-import`、`decision-import`，均用 `file --db state.sqlite`。正常 fixture 在 `fixtures/*-ok.json`，权属错配、封签破损、无通知绝当的反例在 `fixtures/*-bad.json`。每份来源文件原字节与 SHA256 入库，`audit` 在空库重放并比较全部业务状态。

实验室离线采录：`lab-capture-import` 录入两台光谱仪的原始峰列、空白/标样校准、样品盘槽位日志、封签扫描与分析员签名（不接仪器网络）；`lab-evidence-import` 补录独立封签录像或盘位记录；`lab-evidence-revoke` 撤销该证据；`lab-certificate-issue` 按事前授权路线签发认证（`video_primary`＝完整封签录像加一台通过校准的主仪器，`dual_instrument`＝两台仪器独立有效结果一致）。样品身份只沿盘位时间线与封签约束归属：盘位缺帧时枚举全部可行全局解释，两个解释对真伪结论不一致就拒发确定认证；受污染校准批次只隔离依赖它的运行。宝石贷款可用 `lab_route` 指定路线，认证被撤销时已放款贷款转风险复核（`disputed`），原始决策保留。旧的人工 verdict（`lab-sample-import`/`lab-result-import`）及其来源仍留在报告。

首版仅模拟本地签署身份和物品凭据；不接入真实鉴定、支付、保险或法定通知渠道。金额整数分，输入须为正且精确到分。应用程序只给出本地业务决定，不构成真实金融或法律意见。
