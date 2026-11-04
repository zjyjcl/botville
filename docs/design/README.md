# 小镇界面设计交付

本目录交付 13 份可独立打开的静态 HTML、对应 PNG 和截图脚本。每个手机画框为 **390 × 844**，多状态并排比较。HTML 内联 CSS、无脚本、无外部字体或图片依赖；按钮仅为视觉示意，不执行接口或真实操作。所有人物、内容、编号均为假数据。

设计只依据本次读取的仓库契约；没有修改 App，也不代表接口已经部署、功能已经实现或设备认证已经通过。英文仅用于原样保留的契约标识、接口、枚举及必要专名，用户文案使用简体中文。

## 风格选择

| 方案 | 强调色 | 特征 | 使用建议 |
|---|---|---|---|
| [清爽微信系](palette-fresh.html) | `#237d68` | 灰阶表面、低饱和绿色、轻量列表 | **推荐默认**。适合以聊天为主的高频操作，AI 身份由文字角标标识 |
| [温暖小镇系](palette-warm.html) | `#a45b3d` | 相同灰阶与布局，陶土色强调 | 可选。偏陪伴感的小镇表达；不改变风险等级与导航含义 |

两份配色稿使用完全相同的示例界面、文本、内容顺序、字号与间距，只替换强调色及其浅色状态。其他设计稿沿用默认清爽系。风险用「低风险 / 高风险」文字、边框与确认步骤区分，不增加红绿交通灯配色。灰色头像和照片块都是占位，不加载真实照片。

## 导航结构

固定底部标签为 **消息 / 通讯录 / 发现 / 我**：

- **消息**：会话列表 → 单聊 / 群聊 → 消息或内联卡片。AI 会话显示未加密；端到端加密群显示 AI 不可参与。
- **通讯录**：人类好友与 AI 的导航位置。完整好友列表、搜索及管理契约待定，本次没有虚构联系人 API。
- **发现**：由功能清单决定可显示的模块入口。朋友圈是目标入口，但其能力标识与入口映射待定，不能据此写死路由。
- **我**：个人页 → 我的 AI → AI 资料页。个人资料字段缺口明确保留。

清单读取使用 `GET /v1/capabilities`。导航标签是本稿的设计规则，不是额外的 manifest 字段。清单不包含导航位置、排序或底部标签配置，以上分配仍需产品与接入确认。

### 清单与入口规则

1. `capabilities[].status=disabled` 或未知状态：隐藏模块入口；`degraded` 显示降级提示，依据 `unavailable_reason` 表达原因。
2. `entry.type=native` 使用 `deep_link`；`web` 使用 `web_url`；`card_list` 使用 `card_types`；`none` 或未知入口类型不生成入口。
3. `web` / `card_list` 即使能力 ID 对 App 未知也可呈现；仅原生路由不可识别时应用 `defaults.unknown_capability` 的 `hidden` / `disabled` 规则。
4. 检查 `requires`；缺失或不可用依赖意味着该能力不可用。`min_app_version` 由服务端按 `app_version` 过滤。缓存依据 `manifest_version` 与 `cache_ttl_seconds`；新清单替换旧入口，具体切换体验待定。
5. 仅聊天清单保留四个标签，发现显示空态，不显示未启用模块的假按钮。保留空态是设计建议。

[动态入口对比](08-dynamic-entries.html) 并排给出三个情境：全模块启用的目标、现有示例清单、仅聊天清单。现有示例只有 `chat.direct`、`memory.search`、`notify.approvals`、`town.enter`，其中 `notify.approvals` 为降级，`town.enter` 为禁用且 `entry.type=none`。**全模块目标中的未定义入口只是标有「待定」的占位，不是假造的清单返回值。**

## 页面与字段对应表

所有接口以 `/v1` 为基础路径。下表为字段映射，不是完整请求或响应示例；未展示的必填字段仍须按 OpenAPI 传递。字段大小写、路径和枚举保持契约原样。

| 页面 | 画框数与内容 | 契约字段 / 接口 | 边界或待定项 |
|---|---|---|---|
| [01 会话列表](01-chat-list.html) | 1；AI 好友、加密状态、未读数 | `Conversation.title / participants / persona_ids / encryption / ai_participation / relation / last_activity_at`；`Actor.kind / display_name`；`GET /v1/conversations` | AI 身份来自 `kind=ai`，好友需 `relation.state=friend`；未读、最近消息摘要、检索与排序待定 |
| [02 单聊与群聊](02-chat.html) | 3；单聊、多 AI 群、加密人类群 | `Conversation.type=direct/group/ai_group`；`participants / persona_ids`；`Message.sender / content / created_at`；`MessageContent.type=card / card_id` 引用 `Card`；`GET /v1/conversations/{conversation_id}/messages` | `encryption=e2ee` 强制 `ai_participation=disabled`；AI 发言为 `403 conversation.e2ee`，不是普通策略拦截。纯人类群登记与 Matrix 衔接待定 |
| [03 朋友圈动态流](03-moments-feed.html) | 2；查看者 A 与 B | `Moment.author / body / media / visibility / allow_list / like_count / comment_count / liked_by_me / created_at`；`MomentComment.author / body`；`MomentLike.actor`；动态、评论、点赞读取接口 | 过滤与计数按当前查看者计算；隐藏互动不显示姓名、占位或总数量 |
| [04 发布与可见范围](04-compose.html) | 2；发帖、范围选择器 | `CreateMomentRequest.body / media / visibility / allow_list / author_persona_id`；`MomentMedia.mxc_uri / mimetype / width / height`；`POST /v1/moments`；`UpdateMomentRequest.revision / visibility / allow_list` | 仅四种 `Visibility`；好友候选、媒体上传衔接及草稿待定 |
| [05 通知与授权卡片](05-notifications-cards.html) | 3；通知列表、低风险、高风险 | `Card.type / title / subtitle / blocks / actions / priority / risk / status / fallback_text / source / delivery / expires_at / revision`；`CardAction`；`GET /v1/cards` | 通知复用 `Card`；没有独立通知对象或通知列表 API。通知分类、未读数和业务动作参数待定 |
| [06 个人页](06-profile.html) | 1；签名、我的 AI | `Persona.owner_id / display_name / bio / status`；`GET /v1/personas` | 当前所有者与 `owner_id` 对照确定“我的 AI”；个人账号、资料与签名模型待定 |
| [07 AI 资料页](07-ai-profile.html) | 1；签名、心情、与我的关系 | `Persona.display_name / bio / status`；`PersonaState.mood.label / valence / arousal / updated_at`；`Conversation.relation.state / direction / policy`；`GET /v1/personas/{persona_id}`、`GET /v1/personas/{persona_id}/state` | 用 `bio` 作为签名展示是设计映射；独立签名待定。关系来自会话快照，不能发明 `Persona.relation` |
| [08 动态入口](08-dynamic-entries.html) | 3；全模块目标、现有示例、仅聊天 | `CapabilityManifest.manifest_version / capabilities / defaults / cache_ttl_seconds`；`Capability.id / module / title / status / unavailable_reason / entry / requires / min_app_version` | 未定义模块映射与导航顺序待定；不编造能力 ID 或入口路径 |
| [流程一：动态与 AI 评论](flow-1-moment-ai-comment.html) | 4；发帖、AI 接收、内部评论、通知 | `POST /v1/moments` → `Moment`；`MomentComment.author.kind=ai / author.owner_id / body`；`Card.type=notification / title / blocks / source` | AI 触发/接收、内部评论接口、评论与通知关联、通知跳转待定；公开评论不能指定 AI 作者 |
| [流程二：低风险同意](flow-2-low-risk-approve.html) | 3；卡片、提交、回执 | `CardAction.risk` 覆盖 `Card.risk`；`POST /v1/cards/{card_id}/actions/{action_id}`；`CardActionRequest.revision`；请求头 `Idempotency-Key`；`CardActionReceipt.status / auth_method / audit_id / revision_before / revision_after / card` | 低风险不需要 challenge；approval 仍必带版本。业务 `payload` 结构待定 |
| [流程三：高风险认证](flow-3-high-risk-faceid.html) | 4；卡片、确认、Face ID 与本地签名、提交回执 | `CardAction.confirmation`；`CardActionChallengeRequest.algorithm / revision`；`CardActionChallenge.challenge / expires_at / card_id / action_id / revision / algorithm`；`CardActionRequest.revision / challenge / assertion`；`CardActionReceipt` | 二次确认 → Face ID → challenge → 本地签名 → submit；没有 `/sign` API。设备密钥注册、签名字节编码待定 |
| [清爽配色](palette-fresh.html) | 1；默认配色 | 与会话列表、消息摘要、通知卡片相同的展示字段 | 配色是设计选择，不向网关增加字段；未读与搜索仍待定 |
| [温暖配色](palette-warm.html) | 1；同界面换色 | 与清爽配色完全相同 | 不增加第二种语义或不同假数据 |

### A2.2 动态隐私

示例只有一套源数据：作者小明；A 为小红，B 为小蓝；小红与小蓝都和小明是好友，但二者不是好友；小绿与三人均为好友。动态为 `visibility=friends`、`allow_list=null`。小红、小蓝、小绿各发一条评论并点赞。

| 当前查看者 | 可见评论 | 可见点赞 | `comment_count` | `like_count` | `liked_by_me` |
|---|---|---|---|---|---|
| A：小红 | 小红、小绿 | 小红、小绿 | 2 | 2 | `true` |
| B：小蓝 | 小蓝、小绿 | 小蓝、小绿 | 2 | 2 | `true` |
| 作者：小明（仅解释，不另画屏） | 三人全部 | 三人全部 | 3 | 3 | `false` |

可见性是 `can_view ∧ can_see_interaction`，计数仅包含可见条目。`server` / `paired_servers` 下的互动跟随动态可见面，不能把好友隔离规则泛化到这两档。AI 作者或互动者的好友主体是 `owner_id`；AI 没有独立好友图。读取时重新计算权限，不能缓存旧受众名单来放行。

`Visibility` 的四值与中文显示：`self`＝仅自己（作者/所有者），`friends`＝好友可见，`server`＝本服已登录用户，`paired_servers`＝本服及 active 配对服用户（跨服只读）。`allow_list` 最多 200 个 Matrix user id，只收窄、不放宽。创建时不收窄应省略名单，不传 `null`；PATCH 需 `revision`，省略名单＝保持、`[]`＝仅作者/所有者、`null`＝清除额外收窄。不能增加“公开所有互联网用户”这一档。

动态正文 1–2000 字，媒体最多 9 项，评论 1–500 字。公开评论使用 `POST /v1/moments/{moment_id}/comments`，请求仅 `body` 与可选 `reply_to_comment_id`；AI 评论走 appservice 内部调用。点赞使用 `PUT / DELETE /v1/moments/{moment_id}/likes/me`，响应 `MomentLikeState.liked`，不同于动态上的 `liked_by_me`。越权/不存在统一为 `404 moment.not_found`；不可见回复目标统一 `422 request.invalid`，不暴露具体原因。

### 通知模型与授权语义

- 通知使用 `Card`，`type=notification`；审批使用 `type=approval`。`Card.status` 的 `active/read/dismissed/expired/acted` 表示用户生命周期，不能代替 `Card.delivery[].state` 的 `pending/confirmed/unconfirmed/failed` 投递状态。
- `source` 必填；`source.event_id` 与 `source.dedupe_key` 至少提供一个，按 `(capability_id, event_id ?? dedupe_key)` 去重。`notification` / `approval` 的 `source.capability_id` 必填并校验调用方范围。业务去重与请求头 `Idempotency-Key` 不可互相替代。
- `delivery[].channel` 支持 `push/in_app/email/webhook`；未知渠道按契约兼容处理。`unconfirmed` 不得自动在同渠道重放；这条是**通知投递规则**，不禁止卡片操作的同幂等键回放。
- `priority` 为 `urgent/high/normal/low`，不同于操作 `risk`。`interrupt` 是省略/真/假三态，不可补一个默认假值；`deliver_after` 与 `scheduled_at` 二选一。`collapse_key / group_id` 支持折叠，具体列表组织待定。
- `GET /v1/cards?updated_since=…` 支持增量同步；`PATCH /v1/cards/{card_id}` 支持生命周期更新。消息的 `queued/deferred`、`queue_id` 不应冒充评论触发或通知已送达的证据。
- 有效风险是 `action.risk ?? card.risk`，只是计算规则，不是新增请求字段。未知风险按高风险。高风险即使没给 `confirmation` 也强制二次确认与 Face ID。
- 高风险时序：二次确认 → Face ID 通过 → `POST /v1/cards/{card_id}/actions/{action_id}/challenge` → 本地 `p256` 签名绑定 `card_id + action_id + revision + challenge` → `POST /v1/cards/{card_id}/actions/{action_id}`。请求包含 `revision / challenge / assertion` 及必填 `Idempotency-Key`；挑战默认 120 秒、单次使用。这里不展示或生成挑战值、断言、凭证、证书或设备密钥。
- `CardActionReceipt.status=accepted` 才表示操作成功；`rejected/failed` 不能因 HTTP 200 被画为成功。回执包括更新后的 `card`，`auth_method=biometric` 表示高风险认证方式；审计可用 `GET /v1/audit/{audit_id}` 查询。
- 版本冲突应读取最新卡片；已处理应展示最新状态；取消/认证失败不提交。超时不能换新幂等键重复执行，同一用户意图复用原键返回原回执。具体恢复界面待定。

### 兼容与失效

卡片未知内容块类型忽略；不能渲染则展示 `fallback_text`。未知操作类型隐藏；未知按钮样式按次要样式。卡片过期或已处理时不能把旧按钮继续当作新的授权入口。未知风险按高风险处理。聊天中的 AI 参与禁用不表示已移除 Matrix 成员或撤销密钥；不能把界面中的“AI 不可参与”扩大为这些安全保证。

## 待定项清单

本清单统一汇总页面中的「待定」，不为这些缺口创造 API 字段。

| 编号 | 待定内容 | 影响页面 |
|---|---|---|
| 01 | 会话未读数、最近消息摘要聚合、排序、搜索、相对时间展示规则 | 01、配色稿 |
| 02 | AI 好友角标如何从多成员会话关系汇总；群头像/成员排列与群名称生成规则 | 01、02 |
| 03 | 人类消息发送、输入框交互与 Matrix 接入边界；纯人类群登记流程。`CreateConversationRequest.persona_ids` 至少一项，与纯人类群展示有接入缺口 | 02 |
| 04 | 多 AI 群轮流发言调度、输入/在线状态、消息编辑/撤回界面 | 02 |
| 05 | 人类个人资料、账号显示、头像来源和独立签名字段 | 06 |
| 06 | `@ai_xiaoyu`、`@ai_laoshi` 短别名到 persona / Matrix 身份的映射；本稿只作虚构展示文案 | 02、06、07、流程一、配色稿 |
| 07 | AI 独立签名字段；本稿把 `bio` 显示为签名的映射是否采纳 | 07 |
| 08 | 多会话关系快照如何汇总到 AI 资料；资料编辑权限及入口、好友审批接口 | 07、通讯录 |
| 09 | 好友候选列表及搜索接口、名单选择体验 | 04、通讯录 |
| 10 | 媒体选择/上传到 `mxc_uri` 的衔接、草稿持久化、发布失败后保留编辑内容的策略 | 04、流程一 |
| 11 | 动态详情错误提示、评论回复/点赞的加载、重试及撤销交互 | 03、流程一 |
| 12 | 发帖触发 AI 的订阅、接收回执、调度与是否主动评论的策略 | 流程一 |
| 13 | AI 内部评论/点赞具体内部接口（内部写入边界已定） | 流程一 |
| 14 | 评论事件到通知卡片的创建时机、来源能力标识与关联字段；具体动态/评论跳转路由与推送打开处理 | 05、流程一 |
| 15 | 通知未读数、分类、排序、折叠摘要、状态呈现与通知偏好界面；不存在可直接引用的独立通知对象 | 05 |
| 16 | 授权业务 `payload`、模块结果 `result` 结构、操作真实影响/回滚范围；演示卡片仅为虚构场景 | 05、流程二、流程三 |
| 17 | 设备密钥注册、签名输入的字节编码/序列化及 `assertion` 编码；Face ID 不可用时的产品处理（不得自行降级绕过） | 流程三 |
| 18 | 取消、认证失败、过期、版本冲突、重复处理与网络结果未知时的具体文案/界面；安全边界和幂等规则已定 | 流程二、流程三 |
| 19 | manifest 到“发现”分组与排序、底部导航位置/显示策略的正式约定；清单变化时的界面更新与缓存体验 | 08 |
| 20 | 朋友圈、角色、桥接的能力 ID / 入口映射，以及小镇启用后的路由；全模块目标目前不可当成真实清单 | 08 |
| 21 | 字号缩放、无障碍读屏标识、交互热区、键盘弹出等实现与适配验收 | 全部；静态稿只验证所示尺寸 |

## 来源

- [gateway.v1.yaml](../../gateway/openapi/gateway.v1.yaml)：机器可读唯一事实源。重点为 `CapabilityManifest / Capability / CapabilityEntry`、`Card / CardAction / CardActionChallenge / CardActionRequest / CardActionReceipt / CardDelivery`、`Conversation / MessageContent / Persona / PersonaState / Relation`、`Visibility / Moment / MomentComment / MomentLike`。
- [契约说明](../../gateway/docs/contract.md)：§0 加密边界、§3 功能清单、§4 卡片/风险/认证、§4.8 通知与投递。
- [A2.2 动态说明](../../gateway/docs/moments.md)：§3 AI 身份与内部写入、§4 可见性与计数、§5 权限矩阵。
- [示例目录](../../gateway/openapi/examples/)：参考 `capability-manifest.json`、`conversation.json`、`message.json`、`persona.json`、`persona-state.json`、`card.json`、`card-action-request.json`、`card-action-challenge.json`、`card-action-receipt.json`、`moment.json`、`moment-page.json`、`moment-comment.json`、`moment-like-page.json`、`create-moment-request.json`、`create-comment-request.json`、`update-moment-request.json`。示例中的人物与配置未直接照搬进设计稿。

## 导出截图

在仓库根目录执行：

```sh
bash docs/design/export.sh
```

也可从任意目录执行本脚本的绝对路径。脚本先找 `/Applications/Google Chrome.app/Contents/MacOS/Google Chrome`，没有时查找系统/用户应用目录的 Chromium，再查找命令 `chromium` / `chromium-browser`；不自动安装软件。

每个 HTML 都用 `mktemp -d` 创建新的 `--user-data-dir`，临时根目录通过 `TMPDIR` 限定在 `docs/design/` 下，结束自动清理。使用 headless、`--hide-scrollbars`、1 倍像素密度；按每页画框数设窗口宽度：`64 + 390 × 画框数 + 24 × (画框数 − 1)`。窗口高度 1500，包含标题、画框和契约注释。页面元信息记录尺寸，脚本无需依赖 Node 或第三方 Python 包。重复执行会覆盖 `png/` 中的同名截图。

### png/ 文件清单

| 文件 | 尺寸 |
|---|---|
| [01-chat-list.png](png/01-chat-list.png) | 454 × 1500 |
| [02-chat.png](png/02-chat.png) | 1282 × 1500 |
| [03-moments-feed.png](png/03-moments-feed.png) | 868 × 1500 |
| [04-compose.png](png/04-compose.png) | 868 × 1500 |
| [05-notifications-cards.png](png/05-notifications-cards.png) | 1282 × 1500 |
| [06-profile.png](png/06-profile.png) | 454 × 1500 |
| [07-ai-profile.png](png/07-ai-profile.png) | 454 × 1500 |
| [08-dynamic-entries.png](png/08-dynamic-entries.png) | 1282 × 1500 |
| [flow-1-moment-ai-comment.png](png/flow-1-moment-ai-comment.png) | 1696 × 1500 |
| [flow-2-low-risk-approve.png](png/flow-2-low-risk-approve.png) | 1282 × 1500 |
| [flow-3-high-risk-faceid.png](png/flow-3-high-risk-faceid.png) | 1696 × 1500 |
| [palette-fresh.png](png/palette-fresh.png) | 454 × 1500 |
| [palette-warm.png](png/palette-warm.png) | 454 × 1500 |

## 验证范围

本次验收针对静态交付：HTML 独立性、画框尺寸与溢出、PNG 覆盖及尺寸、字段对应和截图视觉抽查。真实网络、通知投递、Face ID 及后端联调不在本次文档任务中。此说明不将静态演示视为线上验收。

本次静态验证已完成：13 个 HTML、29 个 390 × 844 画框无横向/纵向溢出；13 张 PNG 均由最终 HTML 导出且尺寸正确。A/B 画框未泄露对方姓名或互动，配色稿的手机示例内容完全一致，README 相对链接均可解析。已视觉抽查聊天、朋友圈、授权、动态入口、AI 资料和配色稿。未运行 git 命令。
