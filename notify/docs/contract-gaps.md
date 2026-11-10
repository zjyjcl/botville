# D 线契约缺口清单

> 日期：2026-11-10（Asia/Shanghai）。基于 `origin/main`（当时为 a2889d2）。
> License: Apache-2.0。纯文档梳理：不改实现、不碰中枢 / 微信 / 个人通道（iMessage 只属个人部署）。
> `docs/migration/deferred.md` 已搁置的迁移内容不纳入推进项。
> 每项四要素：**缺什么** / **谁依赖** / **建议的默认方案** / **出处**。

## 0. 范围与方法

全仓检索「待定 / TBD / 待拍板 / TODO / 未定 / 未决 / 待补充」等，并交叉阅读：

- `#122` 合入的设计文档：`docs/design/`（尤其 [README.md](../../docs/design/README.md) 待定项清单 01–21，以及 `05-notifications-cards.html`、流程一/二/三）
- `docs/contracts/`（目前仅扫码配对，属 A/C 线）
- `notify/`（D1.1 已合入的数据模型与 OpenAPI）
- `gateway/docs/contract.md` 中与通知、授权、审计、开放 API 相关的章节（§1.4.1、§4、§8.2–8.3）
- `docs/migration/D0.1-hub-inventory.md` 的开源替代语义（不推进已归档问题）
- `docs/roadmap.md` 的 D 线任务：D1.1 已交付；D2.1 / D2.2 / D2.3 / D2.5 / D4.2 尚无独立契约；D1.2 / D2.4 / D3.1 属中枢迁移，已搁置

分类：

- **A 类**：可由 D 线直接定稿的小缺口。本次改动已把其中文档不一致、以及可由既有 wire 推出的展示约定写成正式文字；仅写推荐、尚未落笔的项会单独标明。
- **B 类**：产品性选择。所有者已于 **2026-11-10** 按原推荐全部拍板；本节改为已定记录，定论已写入相应说明文档。A6 与 B4 为已定方向，待 D2.1 专门 PR 落地（不改网关 YAML）。

## 1. 已关闭或不纳入

| 项 | 说明 |
|---|---|
| [#74](https://github.com/zjyjcl/botville/issues/74) | **已关闭**（GitHub `closed_at` 为 2026-11-03 20:26 UTC，即 2026-11-04 04:26 Asia/Shanghai）。main 上的合入证据是 `591535b`（#76）。属 B 线，本清单只引用；`notify/docs/data-model.md` §1.5 第 4 条在本次改动中改为「已关闭」。 |
| [#75](https://github.com/zjyjcl/botville/issues/75) | **仍开放**。见第 5 节工程后续。本次改动为保持纯文档/契约，不改 CI workflow；建议另开小 PR 修。 |
| `docs/contracts/` | 仅有配对契约（`pairing-qr.md` / `pairing-redeem.openapi.yaml`），检索到的「占位」均为示例域名与 token 占位，**无 D 线待定项**。 |
| 设计稿待定 01–13、18、19、21 | 会话未读/搜索、人类资料、动态发布、manifest 导航、恢复界面文案、无障碍等，属 A/B/C 线，不在本清单展开。18 的安全边界与幂等规则已在契约定稿，只剩 C 线文案/界面。 |
| D1.2 / D2.4 / D3.1 及 deferred.md | 并行期双发、授权派工从中枢迁走、中枢下线与回滚：按所有者决定搁置，不作为本轮契约缺口推进。 |
| D4.1 小镇网页版 | 属 D 线任务，但是实现/前端工作，**不是契约缺口**，不在本清单展开。 |
| `usage_events` 与小镇账单口径（contract §8.2 D 线第 4 条） | 口径已由 `contract.md` §6.2 定义；D4.2 只做开放文档汇聚，当前无新的契约缺口。 |

## 2. A 类：可由 D 线直接定稿

### A1. `interrupt` 在通知数据模型中的错误默认值

- **缺什么**（修正前）：`notify/docs/data-model.md` §2.5 将 `interrupt` 写成「默认 `false`」，与同一文档 §1.5 / 优先级表、以及权威来源 `contract.md` §4.8（三态、**无默认值**）冲突。
- **谁依赖**：D1.1 通知模型、B 线静默检查、C 线通知展示与打断。
- **建议的默认方案（本次已落笔）**：与 §4.8 对齐——`interrupt` 为省略 / `true` / `false` 三态，schema 与文档都不得补 `default: false`；省略时 `urgent` 穿透，其余优先级顺延；显式 `false` 时连 `urgent` 也不穿透。
- **出处**：`notify/docs/data-model.md` §2.5（原文「默认 `false`」）；`gateway/docs/contract.md` §4.8「静默、优先级与合并」；`gateway/openapi/gateway.v1.yaml` 中 `Card.interrupt` / `CreateCardRequest.interrupt` 的 description（#74 修复后）。

### A2. §1.5 第 4 条仍把 #74 写成未修分歧

- **缺什么**（修正前）：D1.1 缺口表仍写「gateway YAML 该句留给 B0.1 owner 修订」，与当时已合入的 #74/#76 不符。
- **谁依赖**：后续评审与 D/B 线对齐，避免重复开 issue。
- **建议的默认方案（本次已落笔）**：将该行改为「已关闭（#74 / #76）」，并保留正确语义摘要。
- **出处**：`notify/docs/data-model.md` §1.5 第 4 条；issue #74；main 合入 commit `591535b`（#76）。

### A3. 通知列表展示：不新增对象或接口（仅未读与折叠）

- **缺什么**：设计稿写「没有独立通知对象或通知列表 API」，同时把未读数、分类、排序、折叠列表组织标为待定（设计待定 15；`collapse_key / group_id` 的「具体列表组织待定」）。
- **谁依赖**：C3.1 通知列表（`GET /cards`）、C 线角标；D1.1 已有字段足够表达。
- **建议的默认方案（本次已写入 `data-model.md` §2.7；仅包含可由既有 wire 推出的两项）**：
  - 列表与详情继续复用 `Card` 与 `GET /cards`（基路径 `/v1`，含 `updated_since`），不另建通知资源。
  - **未读**：`Card.status == active` 计为未读；`read` / `dismissed` / `expired` / `acted` 不计。未读不是 `delivery[].state`。
  - **折叠**：同 `group_id` 收成一条摘要入口；同 `collapse_key` 的未读卡由新卡替换旧卡展示（已有 §2.4）。折叠只影响展示。
- **出处**：`docs/design/README.md` 待定 15、通知模型节（约第 108、138、169 行）；`docs/design/05-notifications-cards.html` 脚注；`notify/docs/data-model.md` §2.3–2.4、§2.7；`contract.md` §4.8。
- **边界**：分类、排序与偏好已于 2026-11-10 拍板，见 B2；定论已补入 `data-model.md` §2.7。

### A4. 授权 `payload` / `result` 不设全局 schema

- **缺什么**：设计待定 16 把「授权业务 `payload`、模块结果 `result` 结构」以及「操作真实影响/回滚范围」列为待定。网关已把 `payload` / `result` 定义为由来源模块解释的不透明对象。
- **谁依赖**：C3.1 按钮回传、D2.1 审计快照、D2.2 派工、B 线模块回调。
- **建议的默认方案（本次已在 `data-model.md` §2.6 补写）**：v1 **不**规定全局业务 `payload` / `result` schema；结构由发出该卡的模块定义。`payload` 与 `context.metadata` 遵守已有禁令（不得含密钥或个人敏感信息，服务端扫描、命中拒绝，contract §4.4 / §4.6 规则 5）。`result` **建议**同样不放密钥或个人敏感信息，但网关暂无扫描规则，责任在来源模块/D2.1。演示卡的字段仅为虚构，不构成契约。设计待定 16 中的「操作真实影响/回滚范围」属各业务模块的产品语义，**不在 D 线通知契约范围**，由 D2.2 等模块文档自行说明。
- **出处**：`gateway/docs/contract.md` §4.4 `payload`、§4.6 规则 5；`gateway.v1.yaml` `CardAction.payload`、`CardActionRequest.payload`、`CardActionReceipt.result`、`AuditRecord.result`；`docs/design/README.md` 待定 16 与流程二说明。

### A5. 开放 API 总文档与现有契约的对应关系

- **缺什么**：`contract.md` §8.2 要求「本契约与未来 `docs/openapi/` 的对应关系清晰（D 线负责总文档）」，但仓库中原先不存在 `docs/openapi/`。D4.2（小镇开放 API 文档）在 M4，现在就写小镇 API 会超前。§8.3 第 5 条也指出小镇接口尚未纳入网关契约。
- **谁依赖**：对外开源承诺、D4.2、各线评审。
- **建议的默认方案（本次已新增 `docs/openapi/README.md`）**：本目录先作为索引，声明当前事实源仍是分域文件（网关 / 通知 / 配对）；小镇 API 尚未纳入（§8.3 第 5 条），待 A4.1/A4.2 之后由 D4.2 汇入（任务编号以 roadmap 为准；contract 该条写作「A4.1/B4.2」）。`usage_events` 与小镇账单口径已由 `contract.md` §6.2 定义，D4.2 只做文档汇聚，不另定口径。现阶段不平行维护一份易漂移的拷贝。版本规则沿用 `contract.md` §5。
- **出处**：`gateway/docs/contract.md` §8.2 D 线清单、§8.3 第 5 条、§6.2；`docs/roadmap.md` D4.2；`docs/README.md` 原 OpenAPI 索引段。

### A6. D2.1 断言字节编码（已定方向，待 D2.1 PR 落地）

- **缺什么**：高风险授权的绑定字段、算法枚举（仅 `p256`）、challenge 默认 120 秒且单次使用、无 `/sign` API，均已在网关契约定稿。`assertion` 的字节编码与设备密钥注册/吊销接口此前未写入网关 YAML。
- **谁依赖**：D2.1、C3.1（Face ID + 本地签名）、B 线校验 assertion。
- **已定方向（所有者 2026-11-10 拍板；待 D2.1 专门 PR 写入 gateway YAML，本次不改 schema）**：
  - 签名输入为四个绑定字段的规范 JSON 对象（键序：`card_id`、`action_id`、`revision`、`challenge`），`revision` 为 JSON 数字，字符串字段按 UTF-8，无无意义空白；整体 UTF-8 编码后签名。
  - `assertion` 传 **base64url（无填充）**，内容为 P-256 ECDSA 签名的 raw `r || s`（各 32 字节，共 64 字节）；算法标识继续用已有 `p256`。
  - 设备密钥注册 / 多设备 / 吊销见 **B4**（同样待 D2.1 PR 用 minor 增加接口）。
- **落笔**：方向已写入 `notify/docs/data-model.md` §2.6，**不**改 `gateway.v1.yaml`。
- **出处**：`gateway/docs/contract.md` §4.4；`gateway.v1.yaml` `createCardActionChallenge` / `CardActionRequest.assertion`；`docs/design/README.md` 待定 17 与流程三。

## 3. B 类：已定（所有者 2026-11-10 拍板）

以下全部按原推荐定稿。可落笔的规则已写入 `notify/docs/data-model.md`（及 README / 设计索引）；A6 与 B4 只记方向，待 D2.1 专门 PR 改网关 YAML。

### B1. 评论事件如何变成通知卡，以及跳转

- **缺什么**：评论通知的创建时机、`source.capability_id`、与动态/评论的关联字段、App 内跳转与推送打开路由原先未定义。
- **谁依赖**：C 线通知列表与推送打开；A/B 线动态评论写入；D 线通知生产者约定。
- **已定方案**：评论在对接收者**可见之后**才建卡（遵守动态可见性，不可见者不通知）；`type=notification`；`capability_id` 用已有 `notify.inbox`（不必先新增 `notify.moments`）；关联放在已有 `context.metadata`（如 `moment_id` / `comment_id`）与 `context.deep_link`，不新增通知对象或关联字段；推送打开走该 deep link，由 C 线映射到动态详情。若社交域希望能力归 moments 模块，再单开 minor 增加能力 ID。
- **落笔**：`notify/docs/data-model.md` §2.8。
- **出处**：`docs/design/README.md` 待定 14、流程一；`docs/design/05-notifications-cards.html`；落笔见 `notify/docs/data-model.md` §2.8（capability 约定见 §4）。

### B2. 通知分类、排序与偏好的产品形态

- **缺什么**：设计待定 15 中的分类、排序、通知偏好；`GET /cards` 只定义过滤、不规定排序。
- **谁依赖**：C 线通知列表与设置页。
- **已定方案**：M2/M3 **不**做独立通知偏好 API；系统级打扰交给操作系统通知设置与已有 `SilentHours`。分类按 `type` 与 `source.capability_id` 分组、不另作枚举。客户端排序按 `priority` 序（`urgent > high > normal > low`），同级按 `updated_at` 降序。若日后要做每能力开关，再走网关 minor。
- **落笔**：`notify/docs/data-model.md` §2.7。
- **出处**：`docs/design/README.md` 待定 15；`contract.md` §4.8；`GET /cards`；`SilentHours`。

### B3. 高风险操作时设备上 Face ID / 生物识别不可用

- **缺什么**：设计待定 17「Face ID 不可用时的产品处理（不得自行降级绕过）」原先未定。
- **谁依赖**：C3.1 授权按钮；D2.1。
- **已定方案**：有效 risk=high 时 **fail-closed**：不降级为仅点按或密码代替签名，不提交操作；提示改用已登记生物识别的设备，或取消。
- **落笔**：`notify/docs/data-model.md` §2.6。
- **出处**：`docs/design/README.md` 待定 17、流程三；`contract.md` §4.4。

### B4. 设备密钥注册、多设备与吊销

- **缺什么**：网关没有注册、列举、吊销设备密钥的接口。
- **谁依赖**：D2.1、C3.1、多设备登录。
- **已定方向（待 D2.1 专门 PR 落地，本次不改网关 YAML）**：用网关 minor 增加「注册 / 列举 / 吊销本所有者设备签名公钥」的接口；每台设备各自注册，吊销立即导致该设备 assertion 失败；密钥物料不得进仓库或日志。具体 UI 与是否强制 App Attest 由 C 线在该 PR 一并评审。
- **落笔**：方向已写入 `notify/docs/data-model.md` §2.6；接口路径待 D2.1 PR。
- **出处**：`docs/design/README.md` 待定 17；`contract.md` §4.4；`gateway.v1.yaml` 接口总览。

### B5. `queued` / `deferred` 之后如何通知所有者

- **缺什么**：`contract.md` §8.3 第 2 条原先待 D 线推送方案。
- **谁依赖**：B2.5 主动发言、C 线推送（A1.2 Sygnal / NSE）、D 线通知卡。
- **已定方案**：**不另建一条 D 线推送通道**。会话消息的到达继续走 Matrix 推送（A1.2 Sygnal + App NSE）。`queued` / `deferred` 的客户端行为保持现状：拿 `queue_id` 等待，不自行重试。仅当需要可操作的应用内卡片（授权、结果、提醒）时，再按 D1.1 创建 `Card`。
- **落笔**：`notify/docs/data-model.md` §2.9。
- **出处**：`gateway/docs/contract.md` §8.3 第 2 条、§1.4.1、§4.8。

### B6. 可选外部通知插件（D2.5）的形态

- **缺什么**：D2.5 尚无接口请求体细节；产品形态原先未定。
- **谁依赖**：出站通知扩展；私人渠道实现不进开源。
- **已定方案**：v1 只做出站 webhook：对已投递/状态变化的 `Card` 快照 POST 到所有者配置的 URL；鉴权用部署侧密钥**引用**（不把真实密钥写入仓库或开源示例）；重试遵循 D1.1「仅 `failed` 可同渠道重试、`unconfirmed` 不重放」。示例插件保持中性，不含任何具体私人渠道。入站私人渠道不进开源仓库。注册与鉴权请求体由 D2.5 PR 补充。
- **落笔**：`notify/docs/data-model.md` §4.3、`notify/README.md`。
- **出处**：`docs/roadmap.md` D2.5；`contract.md` §8.2；`notify/README.md`。

### B7. 接入 Codex / OpenClaw（D2.2）的事件与授权 payload

- **缺什么**：完成监听如何变成通知卡、哪些动作需要授权卡、业务 payload 形状原先无开源约定。
- **谁依赖**：D2.1 授权链、C3.1、B 线接入 SDK。
- **已定方案**：完成/失败事件 → `type=notification` + `notify.inbox`；需要所有者批准的派工 → `type=approval` + `notify.approvals`，走既有 challenge / assertion 链；`payload` 只放不透明业务句柄（如 `run_id`），不设全局派工 schema（见 A4）。订阅方式在 D2.2 实现 PR 里写，默认优先事件推入、轮询仅作兜底。不推进已搁置的中枢侧迁移。
- **落笔**：`notify/docs/data-model.md` §4.1。
- **出处**：`docs/roadmap.md` D2.2。

### B8. 桥接模块（D2.3）默认范围与能力 ID

- **缺什么**：飞书 / Telegram / QQ 的能力 ID、入口映射、消息与卡片的对应关系原先未定。
- **谁依赖**：C 线发现入口；D2.3 实现；A1.3 appservice。
- **已定方案**：开源默认支持 **飞书与 Telegram**；QQ/NapCat 作为可选桥，**直连、不走境外代理**（验收含代理检查）。能力 ID 预留 `bridge.feishu`、`bridge.telegram`、`bridge.qq`（属 `bridge` 模块，不进 `notify.*` 枚举）。私人渠道实现不进开源仓库。先做进/出站消息与 Matrix 会话映射；只有需要拍板或摘要时才建 `Card`。入口映射细节随 D2.3。
- **落笔**：`notify/docs/data-model.md` §4.2、`notify/README.md`。
- **出处**：`docs/roadmap.md` D2.3；`docs/design/README.md` 待定 20。

### B9. 响应侧是否补 `scheduled_at`

- **缺什么**：创建请求可用 `scheduled_at`，但 `Card` 响应只有 `deliver_after`。
- **谁依赖**：C 线提醒展示；B 线排程。
- **已定方案**：暂不改 wire；客户端用 `deliver_after`（及可选 `context.metadata`）展示。若必须区分「期望触发时刻」与「最早投递时刻」，再走网关 minor 给 `Card` 增加可选 `scheduled_at`。
- **落笔**：`notify/docs/data-model.md` §1.5 第 1 条。
- **出处**：`contract.md` §4.8；`notify/openapi/notify.v1.yaml` 中 `ReminderCard`。

### B10. `DeliveryRetryPolicy` 是否上 wire

- **缺什么**：重试上限与间隔是 notify 层规范建议值，原先未决定是否上 `Card` / `CardDelivery`。
- **谁依赖**：各渠道实现、D2.5 webhook 重试。
- **已定方案**：保持实现侧建议值（总尝试 ≤ 3 含首次、间隔 ≥ 300 秒、仅 `failed`），不上卡片 wire。
- **落笔**：`notify/docs/data-model.md` §1.5 第 2 条、§3.2。
- **出处**：`notify/openapi/notify.v1.yaml` `DeliveryRetryPolicy`。

## 4. 本次文档改动范围

| 文件 | 变更 |
|---|---|
| `notify/docs/contract-gaps.md` | 本清单（新增） |
| `notify/docs/data-model.md` | 修正 §2.5 `interrupt`；§1.5 第 4 条改为 #74 已关闭；§2.6 澄清 `payload` 禁令不自动扩到 `result`；新增 §2.7（仅未读与折叠） |
| `notify/README.md`、`docs/README.md` | 增加清单与 `docs/openapi/` 索引 |
| `docs/openapi/README.md` | 新增开放 API 总文档索引（D4.2 预留） |

不改：任何实现代码、`gateway.v1.yaml` schema / 状态码、CI workflow、`docs/design/` 静态稿、中枢/个人通道文档的推进内容。

A1–A5 已落笔。修正前的错误记载（`interrupt` 默认 `false`、#74 仍写未修）以修订后的 `data-model.md` 为准。

## 5. 工程后续（非契约缺口，另开 PR）

### E1. CI 为 notify/gateway 校验显式安装 PyYAML（#75）

- **缺什么**：`notify/scripts/validate_examples.py` 与 `gateway/scripts/validate_openapi.py` 都依赖 PyYAML。lint job 里「Notify examples + gateway contract validation」一步直接调用二者，未先安装；同 job 的「Gateway unit tests」才 `pip install pyyaml`，且在校验步**之后**。当前能绿是因为 runner 镜像碰巧预装，与 #75 描述一致。
- **谁依赖**：D1.1 契约校验、全部 PR 的 lint job。
- **建议的默认方案**：在「Notify examples + gateway contract validation」步开头增加钉选安装（建议 `python3 -m pip install 'pyyaml>=6,<7'`，与现有 gateway 测试步的安装合并或前置）；保持实现代码不动。请另开小 PR 关 #75。本次文档改动不改 workflow。
- **出处**：issue #75；`.github/workflows/ci.yml` 中上述两个 step；两份校验脚本的文件头说明。

## 6. 拍板落笔（2026-11-10）

所有者于 2026-11-10 按原推荐拍板 B1–B10（含 B3 fail-closed 不降级；B8 默认飞书+Telegram，QQ/NapCat 可选且直连不走境外代理，私人渠道不进开源）。在 #156 既有已审提交之后追加文档提交：第 3 节改为已定记录，定论写入 `notify/docs/data-model.md`、`notify/README.md`、`docs/openapi/README.md`，并在 `docs/design/README.md` 待定 14–17、20 处链接到本清单。A6 与 B4 记为「已定方向、待 D2.1 PR 落地」，**不**改网关 YAML、CI 或实现。
