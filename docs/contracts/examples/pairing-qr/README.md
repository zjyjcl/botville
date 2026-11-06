# pairing-qr 契约示例（App fixture 的单一来源）

本目录是 App 扫码配对契约（[../../pairing-qr.md](../../pairing-qr.md)）的 mock
示例，同时是 App 测试 fixture 的**单一来源（single source of truth）**：

- `app-apple/Tests/BotvilleCoreTests/Fixtures/pairing-qr-contract/` 是本目录的
  **生成副本**（文件集合与内容逐字节一致），**勿手改**——漂移会被 CI 守卫拦下；
- 修改示例请直接改本目录，然后运行 `scripts/sync-pairing-qr-fixtures.sh`
  （幂等；App 侧副本会被替换为与本目录一致，多余文件会被删除），两边的改动
  一起提交；
- CI（lint job）以 `scripts/sync-pairing-qr-fixtures.sh --check` 校验两目录
  无漂移，有差异即失败；
- `server/tests/test_pairing_qr_contract.py` 按文件名分类校验本目录每个示例，
  并做占位/密钥扫描。

目录内容：3 个有效 URI（含 `uri-valid-no-gw.txt`，无 gw 配对 = chat-only）、
8 个无效 URI、1 个兑换请求、3 个 200 响应（`redeem-200-no-gateway.json` 不含
`features_url`，见 pairing-qr.md §5.1.1）、5 个错误体。所有域名/token 均为占位
（example.org / `<redacted-example>`）。
