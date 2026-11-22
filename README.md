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
