# docs/openapi/ — 开放 API 总文档（D 线）

License: Apache-2.0（与仓库一致）。

`gateway/docs/contract.md` §8.2 把「本契约与未来 `docs/openapi/` 的对应关系」交 D 线维护。本目录是对外开放 API 的**总文档入口**（路线图任务 [D4.2](../roadmap.md)），不是第二份平行契约。

## 当前事实源（请直接引用，不要在此复制 schema）

在 D4.2 汇聚之前，机器可读契约仍分域存放：

| 文件 | 范围 |
|---|---|
| [gateway/openapi/gateway.v1.yaml](../../gateway/openapi/gateway.v1.yaml) 与 [gateway/docs/contract.md](../../gateway/docs/contract.md) | 网关 v1，**唯一事实源** |
| [notify/openapi/notify.v1.yaml](../../notify/openapi/notify.v1.yaml) 与 [notify/docs/data-model.md](../../notify/docs/data-model.md) | 通知域对网关 `Card*` 的只增语义 |
| [docs/contracts/pairing-redeem.openapi.yaml](../contracts/pairing-redeem.openapi.yaml) 与 [docs/contracts/pairing-qr.md](../contracts/pairing-qr.md) | 扫码配对（A/C 线） |

小镇模块的开放 API 尚未纳入网关契约（见 contract.md §8.3 第 5 条），随 A4.1 / A4.2 在 M4 由 D4.2 写入本目录，并做与实现一致的 CI 校验。

版本规则沿用 [contract.md §5](../../gateway/docs/contract.md)：只增不删，弃用走公告流程。

D 线仍开放的契约缺口见 [notify/docs/contract-gaps.md](../../notify/docs/contract-gaps.md)。
