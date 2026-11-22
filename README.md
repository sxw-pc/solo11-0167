# 典当质押赎回采证台

离线的质押担保数据采录台。权属估价决定放款上限；库房封签和独立盘点决定担保物是否完整；还款、展期与通知决定赎回或绝当资格；继承采录与实物交接决定结清质物实际由谁取得。四本账各有 CLI、持久实体、状态变化和失败样例。所有人物与物品都是合成数据。

```powershell
python -m pip install . --no-deps --no-build-isolation
python -m pawnproof demo --db demo.sqlite
python -m pawnproof audit --db demo.sqlite
python -m pytest -q
```

单独入口：`asset-import`、`ownership-import`、`appraisal-import`、`vault-import`、`custody-import`、`loan-import`、`payment-import`、`extension-import`、`notice-import`、`decision-import`，均用 `file --db state.sqlite`。正常 fixture 在 `fixtures/*-ok.json`，权属错配、封签破损、无通知绝当的反例在 `fixtures/*-bad.json`。每份来源文件原字节与 SHA256 入库，`audit` 在空库重放并比较全部业务状态。

## 继承/代理赎回与实物交付

`payment-import` 可带 `payer`/`relationship`：借款人死亡后由继承人垫付只清债，付款人不取得质物所有权或领取权。`decision-import kind=redeem` 一旦知道客户身故或路线已固定，会以 `blocked/heir_handoff_required` 拒绝合并“清债 + 放物”；仍在世的借款人柜台流程保持原有行为。

新增采录与交接入口，同样用 `file --db state.sqlite`：

- `customer-import`：本地 mock 的客户身份状态（在生 / 身故，身故不可撤销）。
- `heir-import`：两名继承人的份额陈述；可一次导入 `heirs` 数组。
- `document-import`：授权、代理、撤回、法院保管指令。只有元数据没有正文时记为 `metadata_only`，不算授权。
- `document-body-import`：正文单独后到，补齐同一文件。
- `counter-check-import`：柜台身份核验；法院保管点接收回执也用 `method=court_receipt` 登记。
- `custody-point-import`：登记法院/中立保管点。
- `route-import`：在打开库位前固定唯一路线（`direct_heir` 或 `neutral_custody`），固定后不可更换。
- `dispatch-import`：先落写前意图，再清空库位；库位清空只表示离库，`asset.status` 变为 `held_at_shop`，绝不是 `released`。
- `handover-import`：外部收件回执，绑定唯一交付对象与两名库房人员签收；被拒收时保留 `shop-vault` 为实际保管方。
- `reconcile`：恢复协议。重放“意图已提交但未抬出”“已抬出无回执”“回执已存在但落库失败”“旧路径 released 却无回执”等裂缝，只追加事实，并开出争议与追偿线索。

`report`/`audit` 的 `three_line_status` 分开显示三条互不替代的状态：`debt`（债务清偿）、`claim`（按交付时点重放的合法领取权）、`delivery`（实际交付与实际保管方）。`audit` 会在空库重放全部来源文件并比较状态。

首次仅模拟本地签署身份和物品凭据；不接入真实鉴定、支付、人口、法院、保险或法定通知渠道。金额整数分，输入须为正且精确到分。应用程序只给出本地业务决定，不构成真实金融或法律意见。
