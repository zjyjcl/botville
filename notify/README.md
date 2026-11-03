# notify/ — 通知与授权（D 线）

License: Apache-2.0（与仓库一致）。

- 通知数据模型、投递状态机、优先级与合并、提醒卡片（D1.1，见下）
- 授权回调：签名、有效期、防重放、审计（衔接 B0.1 契约 §4.4，实现属 D2.1）
- 完成通知 / 摘要的开放接口（D2.2 完成监听、D2.5 可选外部通知插件在其上扩展）
- 桥接模块：飞书、Telegram（QQ/NapCat 直连，不走境外代理）

## 目录布局

- `docs/data-model.md` — 开源通知数据模型规范（D1.1 交付，Schema 评审入口）
- `openapi/notify.v1.yaml` — 通知域 schema：`$ref` 聚合 `gateway.v1.yaml` 的
  `Card*` 等，`allOf` 只增通知域语义（类型、来源键、失败码、重试建议值）
- `openapi/examples/` — 通知域示例（wire 时间一律 UTC `Z`，无真实密钥，显示名中性）
- `scripts/validate_examples.py` — 通知域文档 + 示例校验

## 与 B0.1 的关系

通知的 wire 形态由 `gateway/openapi/gateway.v1.yaml` 的 `Card` /
`CreateCardRequest` 定义（行为见 `gateway/docs/contract.md` §4.8）。
D1.1 不改网关契约：`notify.v1.yaml` 只引用、只增，网关校验保持绿。

## 如何校验

```bash
python3 notify/scripts/validate_examples.py   # notify 文档 + 5 个示例（含对网关 schema 的交叉校验）
python3 gateway/scripts/validate_openapi.py   # 网关契约（D1.1 未改动，应保持绿）
```

## 非目标

- 不实现通知发送服务 / 推送 SDK 对接；渠道只抽象到
  `push` / `in_app` / `email` / `webhook` 开放枚举。
- 不做 D1.2（并行期双发与对账已搁置归档，见
  [docs/migration/deferred.md](../docs/migration/deferred.md)）；本模型不依赖它。
- D2.1 签名实现、D2.5 插件实现不在本目录。
- 不含中枢路径、私人渠道实现、私有部署细节（个人部署事项不进本仓库）。

## 迁移盘点（历史）

- 盘点文档：[docs/migration/D0.1-hub-inventory.md](../docs/migration/D0.1-hub-inventory.md)
- 搁置归档：[docs/migration/deferred.md](../docs/migration/deferred.md)
  （中枢迁移与个人通道相关工作按所有者决定全部搁置，暂不执行）
