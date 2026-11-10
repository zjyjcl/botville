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

- **A 类**：可由 D 线直接定稿的小缺口。本 PR 已把其中文档不一致与「不新增 API 的默认映射」写成正式文字。
- **B 类**：产品性选择，需所有者拍板。本文件只列推荐与理由，**不在契约里写成定论**。

## 1. 已关闭或不纳入

| 项 | 说明 |
|---|---|
| [#74](https://github.com/zjyjcl/botville/issues/74) | **已关闭**（completed，2026-11-03）。B 线在 `54b89ac` / `2d3f5dc` 修正 `CreateCardRequest.description`，经 #76 合入。gateway YAML 已与 `contract.md` §4.8 对齐。属 B 线，本清单只引用；`notify/docs/data-model.md` §1.5 第 4 条在本 PR 改为「已关闭」。 |
| [#75](https://github.com/zjyjcl/botville/issues/75) | **仍开放**。见下方 A6。本 PR 为保持纯文档/契约，不改 CI workflow；建议另开小 PR 修。 |
| `docs/contracts/` | 仅有配对契约（`pairing-qr.md` / `pairing-redeem.openapi.yaml`），检索到的「占位」均为示例域名与 token 占位，**无 D 线待定项**。 |
| 设计稿待定 01–13、18、19、21 | 会话未读/搜索、人类资料、动态发布、manifest 导航、恢复界面文案、无障碍等，属 A/B/C 线，不在本清单展开。18 的安全边界与幂等规则已在契约定稿，只剩 C 线文案/界面。 |
| D1.2 / D2.4 / D3.1 及 deferred.md | 并行期双发、授权派工从中枢迁走、中枢下线与回滚：按所有者决定搁置，不作为本轮契约缺口推进。 |

## 2. A 类：可由 D 线直接定稿

### A1. `interrupt` 在通知数据模型中的错误默认值

- **缺什么**：`notify/docs/data-model.md` §2.5 将 `interrupt` 写成「默认 `false`」，与同一文档 §1.5 / 优先级表、以及权威来源 `contract.md` §4.8（三态、**无默认值**）冲突。
- **谁依赖**：D1.1 通知模型、B 线静默检查、C 线通知展示与打断。
- **建议的默认方案（本 PR 已落笔）**：与 §4.8 对齐——`interrupt` 为省略 / `true` / `false` 三态，schema 与文档都不得补 `default: false`；省略时 `urgent` 穿透，其余优先级顺延；显式 `false` 时连 `urgent` 也不穿透。
- **出处**：`notify/docs/data-model.md` §2.5（原文「默认 `false`」）；`gateway/docs/contract.md` §4.8「静默、优先级与合并」；`gateway/openapi/gateway.v1.yaml` 中 `Card.interrupt` / `CreateCardRequest.interrupt` 的 description（#74 修复后）。

### A2. §1.5 第 4 条仍把 #74 写成未修分歧

- **缺什么**：D1.1 缺口表仍写「gateway YAML 该句留给 B0.1 owner 修订」，与仓库现状不符。
- **谁依赖**：后续评审与 D/B 线对齐，避免重复开 issue。
- **建议的默认方案（本 PR 已落笔）**：将该行改为「已关闭（#74 / #76）」，并保留正确语义摘要。
- **出处**：`notify/docs/data-model.md` §1.5 第 4 条；issue #74；commit `54b89ac`、`2d3f5dc`、`591535b`（#76）。

### A3. 通知列表展示：不新增对象或接口的默认映射

- **缺什么**：设计稿写「没有独立通知对象或通知列表 API」，同时把未读数、分类、排序、折叠列表组织标为待定（设计待定 15；`collapse_key / group_id` 的「具体列表组织待定」）。
- **谁依赖**：C3.1 通知列表（`GET /v1/cards`）、C 线角标；D1.1 已有字段足够表达。
- **建议的默认方案（本 PR 已写入 `data-model.md` §2.7，作为通知域默认映射，不新增字段）**：
  - 列表与详情继续复用 `Card` 与 `GET /v1/cards`（含 `updated_since`），不另建通知资源。
  - **未读**：`Card.status == active` 计为未读；`read` / `dismissed` / `expired` / `acted` 不计。未读不是 `delivery[].state`。
  - **分类**：按 `type` 与 `source.capability_id` 分组展示，不另作分类枚举。
  - **排序**：默认 `priority` 序（`urgent > high > normal > low`），同级按 `updated_at` 降序；`GET /v1/cards` 的服务端排序未在网关契约规定，本映射约束客户端默认展示。
  - **折叠**：同 `group_id` 收成一条摘要入口；同 `collapse_key` 的未读卡由新卡替换旧卡展示（已有 §2.4）。折叠只影响展示。
- **出处**：`docs/design/README.md` 待定 15、通知模型节（约第 108、138、151–153 行）；`docs/design/05-notifications-cards.html` 脚注；`notify/docs/data-model.md` §2.3–2.4；`contract.md` §4.8。
- **边界**：通知偏好界面 / 是否另做偏好 API 属 **B2**，本条不拍板。

### A4. 授权 `payload` / `result` 不设全局 schema

- **缺什么**：设计待定 16 把「授权业务 `payload`、模块结果 `result` 结构」列为待定。网关已把二者定义为由来源模块解释的不透明对象。
- **谁依赖**：C3.1 按钮回传、D2.1 审计快照、D2.2 派工、B 线模块回调。
- **建议的默认方案（本 PR 已在 `data-model.md` §2.6 补写）**：v1 **不**规定全局业务 `payload` / `result` schema；结构由发出该卡的模块定义；必须遵守已有禁令（不得含密钥或个人敏感信息，命中则拒绝）。演示卡的字段仅为虚构，不构成契约。
- **出处**：`gateway/docs/contract.md` §4.4 `payload`、§4.6 规则 5；`gateway.v1.yaml` `CardAction.payload`、`CardActionRequest.payload`、`CardActionReceipt.result`、`AuditRecord.result`；`docs/design/README.md` 待定 16 与流程二说明。

### A5. 开放 API 总文档与现有契约的对应关系

- **缺什么**：`contract.md` §8.2 要求「本契约与未来 `docs/openapi/` 的对应关系清晰（D 线负责总文档）」，但仓库中不存在 `docs/openapi/`。D4.2（小镇开放 API 文档）在 M4，现在就写小镇 API 会超前。
- **谁依赖**：对外开源承诺、D4.2、各线评审。
- **建议的默认方案（本 PR 已新增 `docs/openapi/README.md`）**：本目录先作为索引，声明当前事实源仍是分域文件（网关 / 通知 / 配对），小镇 API 待 A4.1/A4.2 之后由 D4.2 汇入；现阶段不平行维护一份易漂移的拷贝。版本规则沿用 `contract.md` §5。
- **出处**：`gateway/docs/contract.md` §8.2 D 线清单末两条；`docs/roadmap.md` D4.2；`docs/README.md` 原 OpenAPI 索引段。

### A6. CI 为 notify/gateway 校验显式安装 PyYAML（#75，本 PR 不改 workflow）

- **缺什么**：`notify/scripts/validate_examples.py` 与 `gateway/scripts/validate_openapi.py` 都依赖 PyYAML。lint job 里「Notify examples + gateway contract validation」一步直接调用二者，未先安装；同 job 的「Gateway unit tests」才 `pip install pyyaml`，且在校验步**之后**。当前能绿是因为 runner 镜像碰巧预装，与 #75 描述一致。
- **谁依赖**：D1.1 契约校验、全部 PR 的 lint job。
- **建议的默认方案**：在「Notify examples + gateway contract validation」步开头增加钉选安装（建议 `python3 -m pip install 'pyyaml>=6,<7'`，与现有 gateway 测试步的安装合并或前置）；保持实现代码不动。请另开小 PR 关 #75。
- **出处**：issue #75；`.github/workflows/ci.yml` 中上述两个 step；两份校验脚本的文件头说明。

### A7. D2.1 断言字节编码（本 PR 只写推荐，不改网关 schema）

- **缺什么**：高风险授权的绑定字段、算法枚举（仅 `p256`）、challenge 默认 120 秒且单次使用、无 `/sign` API，均已在网关契约定稿。**尚未定义** `assertion` 的字节编码与设备密钥如何注册/吊销。contract 与 OpenAPI 明确把编码留给 D2.1。
- **谁依赖**：D2.1、C3.1（Face ID + 本地签名）、B 线校验 assertion。
- **建议的默认方案**（技术默认，供 D2.1 专门 PR 定稿，不在本 PR 写入网关 YAML，以免未经实现评审就改 wire）：
  - 签名输入为四个绑定字段的规范 UTF-8 拼接，推荐固定顺序的 JSON 对象（键序：`card_id`、`action_id`、`revision`、`challenge`），`revision` 为 JSON 数字，字符串字段按 UTF-8，无无意义空白。
  - `assertion` 传 **base64url（无填充）**，内容为 P-256 ECDSA 签名的 raw `r || s`（各 32 字节，共 64 字节），与 Secure Enclave 常见输出对齐；算法标识继续用已有 `p256`。
  - 设备密钥注册 / 多设备 / 吊销接口见 **B4**，需所有者拍板产品范围后再增网关 endpoint（属后续 minor）。
- **出处**：`gateway/docs/contract.md` §4.4「断言格式本身仍是占位……字节编码由 D2.1 定义」；`gateway.v1.yaml` `createCardActionChallenge` description、`CardActionChallenge.algorithm`、`CardActionRequest.assertion`；`docs/design/README.md` 待定 17 与流程三。

## 3. B 类：需要所有者拍板（只推荐，不写成定论）

### B1. 评论事件如何变成通知卡，以及跳转

- **缺什么**：评论通知的创建时机、`source.capability_id`、与动态/评论的关联字段、App 内跳转与推送打开路由均未定义。设计稿已指出评论通知的 `capability_id` 在契约中未定义。
- **谁依赖**：C 线通知列表与推送打开（设计流程一、05 页）；A/B 线动态评论写入；D 线通知生产者约定。
- **推荐**：评论在对接收者**可见之后**才建卡（遵守动态可见性，不可见者不通知）；`type=notification`；`capability_id` 用已有 `notify.inbox`（不必为此先新增 `notify.moments`）；关联放在已有 `context.metadata`（如 `moment_id` / `comment_id`）与 `context.deep_link`，不新增通知对象或关联字段；推送打开走该 deep link，由 C 线映射到动态详情。若社交域希望能力归 moments 模块，再单开 minor 增加能力 ID。
- **理由**：符合「通知复用 Card、不为待定项创造 API 字段」；`notify.inbox` 已在 D1.1 留给收件/完成类通知。
- **出处**：`docs/design/README.md` 待定 14、流程一说明；`docs/design/05-notifications-cards.html`「评论通知的 source.capability_id 在契约中未定义」；`notify/docs/data-model.md` §4。

### B2. 通知偏好与分类的产品形态

- **缺什么**：设计待定 15 中的「通知偏好界面」以及是否要做可配置的分类/免打扰偏好 API（静默时段已有 `SilentHours`，属网关配置）。
- **谁依赖**：C 线设置页；D 线若要新 API 需写入开放文档。
- **推荐**：M2/M3 先**不**做独立通知偏好 API；分类只按 A3 的 `type` / `capability_id` 展示；系统级打扰交给 OS 通知设置 + 已有静默时段。若日后要做每能力开关，再走网关 minor。
- **理由**：避免在 C3.1 之前膨胀设置面；现有字段已能做列表。
- **出处**：`docs/design/README.md` 待定 15；`contract.md` §4.8；`SilentHours` schema。

### B3. 高风险操作时设备上 Face ID / 生物识别不可用

- **缺什么**：设计待定 17 明确「Face ID 不可用时的产品处理（不得自行降级绕过）」未定。
- **谁依赖**：C3.1 授权按钮；D2.1 不得把「可降级」写进协议。
- **推荐**：有效 risk=high 时 **fail-closed**：不降级为仅点按或密码代替签名，不提交操作；提示改用已登记生物识别的设备，或取消。与契约「服务端漏配也不得绕过」「未知风险按高风险」一致。
- **理由**：降级等于打开新攻击面，且与「没有 `/sign` API、必须本地签名」冲突。
- **出处**：`docs/design/README.md` 待定 17、流程三；`contract.md` §4.4 强制生物识别。

### B4. 设备密钥注册、多设备与吊销

- **缺什么**：挑战/断言协议假定已有 Secure Enclave 或 App Attest 设备密钥，但网关没有注册、列举、吊销设备密钥的接口；跨设备如何生效未定。
- **谁依赖**：D2.1、C3.1、多设备登录（C 线会话）。
- **推荐**：D2.1 用网关 minor 增加「注册 / 列举 / 吊销本所有者设备签名公钥」的接口；每台设备各自注册，吊销立即导致该设备 assertion 失败；密钥物料不得进仓库或日志。具体 UI 与是否强制 App Attest 由 C 线在该 PR 一并评审。
- **理由**：没有注册面，服务端无法校验 assertion；这是协议完整性问题，但接口形状与产品范围需要所有者确认后再改网关契约。
- **出处**：`docs/design/README.md` 待定 17；`contract.md` §4.4；`gateway.v1.yaml` 接口总览（无 device-key 路径）。

### B5. `queued` / `deferred` 之后如何通知所有者

- **缺什么**：`contract.md` §8.3 第 2 条：「主动发言的调度与推送通道（`queued` 之后如何通知）待 D 线推送方案确定。」
- **谁依赖**：B2.5 主动发言、C 线推送（A1.2 Sygnal / NSE）、D 线通知卡。
- **推荐**：**不另建一条 D 线推送通道**。会话消息的到达继续走 Matrix 推送（A1.2 Sygnal + App NSE）。`queued` / `deferred` 的客户端行为保持现状：拿 `queue_id` 等待，不自行重试。仅当需要可操作的应用内卡片（授权、结果、提醒）时，再按 D1.1 创建 `Card`。
- **理由**：消息推送已有 A/C 线路径；D 线模型管的是卡片投递（`delivery[]`），两套不应叠成双通道打扰。
- **出处**：`gateway/docs/contract.md` §8.3 第 2 条、§1.4.1、§4.8「queued / deferred 可追踪」。

### B6. 可选外部通知插件（D2.5）的形态

- **缺什么**：D2.5 尚无接口契约。§8.2 问 `source.module` / `capability_id` 是否足以区分外部插件来源——字段层面够用，但插件如何注册、鉴权、收事件未定义。
- **谁依赖**：个人部署的出站通知；明确**不含**微信 / iMessage 等私人渠道实现。
- **推荐**：v1 只做出站 webhook：对已投递/状态变化的 `Card` 快照 POST 到所有者配置的 URL；鉴权用部署侧密钥**引用**（不把真实密钥写入仓库或开源示例）；重试遵循 D1.1「仅 `failed` 可同渠道重试、`unconfirmed` 不重放」。示例插件保持中性，不含任何具体私人渠道。入站私人渠道不进开源仓库。
- **理由**：与 D0.1 / notify README 已写的开源边界一致，先交付最小可用出口。
- **出处**：`docs/roadmap.md` D2.5；`contract.md` §8.2；`notify/README.md` 非目标；`docs/migration/D0.1-hub-inventory.md` F15 / F22 与第 3 节通道原则。

### B7. 接入 Codex / OpenClaw（D2.2）的事件与授权 payload

- **缺什么**：完成监听如何变成通知卡、派工哪些动作需要授权卡、业务 payload 形状，均无开源契约。D2.4（从中枢把派工迁走）已搁置，但开源侧的 App 内闭环仍可独立设计。
- **谁依赖**：D2.1 授权链、C3.1、B 线接入 SDK。
- **推荐**：完成/失败事件 → `type=notification` + `notify.inbox`；需要所有者批准的派工 → `type=approval` + `notify.approvals`，走既有 challenge / assertion 链；`payload` 只放不透明业务句柄（如 `run_id`），不设全局派工 schema（见 A4）。订阅方式（webhook 还是网关内适配器）在 D2.2 实现 PR 里写，默认优先事件推入、轮询仅作兜底。
- **理由**：复用 D1.1 / 授权链，避免为单个生产者发明平行协议。
- **出处**：`docs/roadmap.md` D2.2；`docs/migration/D0.1-hub-inventory.md` F10 / F19（只取开源替代语义，不推进中枢侧迁移）。

### B8. 桥接模块（D2.3）默认范围与能力 ID

- **缺什么**：飞书 / Telegram / QQ 的能力 ID、入口映射、消息与卡片的对应关系未定。设计待定 20 把「桥接的能力 ID / 入口映射」标为待定（同时含朋友圈、角色、小镇，不全是 D 线）。
- **谁依赖**：C 线发现入口；D2.3 实现；A1.3 appservice。
- **推荐**：开源默认支持 **飞书与 Telegram**；QQ/NapCat 作为可选桥，**直连、不走境外代理**（验收含代理检查）。能力 ID 预留 `bridge.feishu`、`bridge.telegram`、`bridge.qq`（属 `bridge` 模块，不进 `notify.*` 枚举）。微信与 iMessage 不进开源。先做进/出站消息与 Matrix 会话映射；只有需要拍板或摘要时才建 `Card`。
- **理由**：与路线图表述和通道原则一致，并避免过早把私人渠道写进开源能力清单。
- **出处**：`docs/roadmap.md` D2.3；`docs/design/README.md` 待定 20；`docs/design/08-dynamic-entries.html`；`docs/migration/D0.1-hub-inventory.md` F14 / F25 与第 3 节。

### B9. 响应侧是否补 `scheduled_at`

- **缺什么**：创建请求可用 `scheduled_at` 表达提醒触发时刻，但 `Card` 响应只有 `deliver_after`。D1.1 已记录该缺口。
- **谁依赖**：C 线提醒展示；B 线排程。
- **推荐**：暂不改 wire；客户端用 `deliver_after`（及可选 `context.metadata`）展示。若提醒必须区分「期望触发时刻」与「最早投递时刻」，再走网关 minor 给 `Card` 增加可选 `scheduled_at`。
- **理由**：避免为尚未出现的 UI 需求改契约；D1.1 已预留这条 minor 路径。
- **出处**：`notify/docs/data-model.md` §1.5 第 1 条；`contract.md` §4.8；`ReminderCard` description。

### B10. `DeliveryRetryPolicy` 是否上 wire

- **缺什么**：重试上限与间隔目前是 notify 层规范建议值，未出现在 `Card` / `CardDelivery` 字段里。
- **谁依赖**：各渠道实现、D2.5 webhook 重试。
- **推荐**：保持实现侧建议值（总尝试 ≤ 3 含首次、间隔 ≥ 300 秒、仅 `failed`），不上卡片 wire，以免每个投递对象都带一份可被客户端误解为可协商的策略。
- **理由**：语义已在数据模型与示例中写清；上 wire 会变成新的兼容面。
- **出处**：`notify/docs/data-model.md` §1.5 第 2 条、§3.2；`notify/openapi/notify.v1.yaml` `DeliveryRetryPolicy`。

## 4. 本 PR 改动范围

| 文件 | 变更 |
|---|---|
| `notify/docs/contract-gaps.md` | 本清单（新增） |
| `notify/docs/data-model.md` | 修正 §2.5 `interrupt`；§1.5 第 4 条改为 #74 已关闭；§2.6 补 `payload`/`result` 不透明约定；新增 §2.7 展示层默认映射 |
| `notify/README.md`、`docs/README.md` | 增加清单与 `docs/openapi/` 索引 |
| `docs/openapi/README.md` | 新增开放 API 总文档索引（D4.2 预留） |

不改：任何实现代码、`gateway.v1.yaml` schema / 状态码、CI workflow、`docs/design/` 静态稿、中枢/个人通道文档的推进内容。
