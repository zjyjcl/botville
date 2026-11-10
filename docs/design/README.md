# 小镇界面设计交付

本目录交付 12 份可独立打开的静态 HTML、对应 PNG 和截图脚本。每个手机画框为 **390 × 844**，多状态并排比较。HTML 内联 CSS、无脚本、无外部字体或图片依赖；按钮仅为视觉示意，不执行接口或真实操作。所有人物、内容、编号均为假数据。

设计只依据本次读取的仓库契约；没有修改 App，也不代表接口已经部署、功能已经实现或设备认证已经通过。英文仅用于原样保留的契约标识、接口、枚举及必要专名，用户文案使用简体中文。

## 最终风格（v2）

采用**主流即时通讯 App 布局**：浅灰背景、白色列表单元格与 1 px 细分隔线；顶部标题居中；底部固定「消息 / 通讯录 / 发现 / 我」。聊天对方为左侧白色气泡，自己为右侧主色气泡，双方均带方形头像；发现页以分组白色列表承载按 manifest 筛选后的入口。

唯一主色为 **`#1AAD8E`**，统一用于己方气泡、主要按钮、AI 角标、选中图标和卡片强调线。AI 好友、心情/签名、授权卡片与好友互动隔离构成小镇的增量。小字号强调文案使用深青绿，主色上的正文使用深色文字。风险仍以「低风险 / 高风险」、说明和确认步骤区分。

[最终设计规范](palette-final.html) 包含色板、字号和组件尺寸。所有长度单位均为 px；手机画框始终为 390 × 844。

| 色值 | 用途 |
|---|---|
| `#1AAD8E` | 唯一主色：己方气泡、主要按钮、AI 角标、选中图标 |
| `#E4F5F0` | 提示与心情的浅色背景 |
| `#087763` | 浅底上的强调文字、辅助图标 |
| `#EDEDED` | 手机内容与画板背景 |
| `#FFFFFF` | 列表行、卡片、对方气泡、搜索框 |
| `#1F2523` | 主文字、主色上的文字、底部手势条 |
| `#6C7471` | 次要文字、时间、未选中导航 |
| `#DDE2DF` | 1 px 分隔线与普通边框 |
| `#F7F8F7` | 底部导航、输入栏、互动列表浅底 |
| `#DCE9E4` | 假头像、照片占位、浅色标签边线 |
| `#B6C0BA` | 手机画框边线 |
| `#A1AAA5` | 未选中单选控件边线 |

| 组件 | 尺寸、圆角与间距 |
|---|---|
| 画框 / 导航 | 390 × 844；状态栏高 28；顶部导航高 60；Tab 栏高 62；底部安全区高 22 |
| 字号 | 画板标题 28；导航标题/姓名 17；聊天正文 15；按钮/正文 14；摘要 13；提示 12；辅助文案/Tab 11；标签 10；AI 角标 9 |
| 行高 | 正文 1.55；气泡 1.5；辅助文案 1.6–1.7 |
| 气泡 | 最大宽 264；内边距上下 10、左右 12；圆角 6；头像间距 10；消息纵向间距 14 |
| 头像 / 图标 | 聊天头像 40 × 40；列表头像 44 × 44，圆角 6；资料头像 64 × 64，圆角 8；Tab 图标 24，普通图标 20 |
| 列表 | 普通行最小高 56，消息行最小高 82；左右内边距 16；分隔线 1；分组间距 14 |
| 卡片 / 按钮 | 卡片内边距 16，圆角 12；按钮最小高 44，圆角 8，双按钮间距 10 |
| 通用间距 | 页面内边距 16；常规纵向间距 14；紧凑流程间距 10；并排画框间距 24 |

规范页为容纳色板采用紧凑示例：卡片内边距 12，气泡正文 14；其余页面使用标准规格。灰色/浅青绿照片和文字头像均为假素材。

## 开源非侵权原则

- 仅借鉴通用的主流即时通讯 App 布局与交互习惯；使用小镇自己的名称、主色、文案与增量功能，不使用第三方品牌名、标识、专有图案或界面截图。
- 所有图标均由本目录的简单几何 SVG 自绘，无外部图标、图片或字体下载。文字头像和照片占位不涉及真实身份或照片。
- 下表的 SF Symbols 名称只说明图标语义对应关系。本稿未复制、提取、打包或分发 SF Symbols 字体或图形；`.fill` 名称也不表示本稿使用该资源。未来原生实现如选用平台资源，须遵循相应平台的许可与使用条件。
- 主色与组件细节独立定义；开源交付只含静态假数据示意，不包含任何密钥、个人资料或真实服务连接。

| 图标位置 / 语义 | 对应 SF Symbols 名称 |
|---|---|
| 底部「消息」、评论输入 | `message.fill` |
| 底部「通讯录」、好友与群组 | `person.2.fill` |
| 底部「发现」、发现空态 | `safari` |
| 底部「我」、资料入口 | `person.crop.circle` |
| 高风险生物认证 | `faceid` |
| 授权卡片、完成回执 | `checkmark.seal` |
| 完成或选中 | `checkmark` |
| 加密会话、隐私说明 | `lock.fill` |
| 通知与提醒 | `bell` |
| 列表行末导航 | `chevron.right` |
| 顶部返回 | `arrow.left` |
| 顶部更多 | `ellipsis` |
| 新建会话、增加内容 | `plus` |
| 图片占位、照片入口 | `photo` |
| 搜索框、记忆检索 | `magnifyingglass` |
| 模块入口 | `square.grid.2x2` |
| 桥接入口 | `link` |
| 设置入口 | `gearshape` |
| 动态点赞 | `heart` |
| 聊天输入语音 | `waveform` |
| 聊天表情、心情 | `face.smiling` |
| 状态栏信号 | `antenna.radiowaves.left.and.right` |
| 状态栏无线网络 | `wifi` |
| 状态栏电量 | `battery.100` |

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
| [最终配色与组件规范](palette-final.html) | 2；最终色板、字号与组件规格 | 与会话列表、消息摘要、通知卡片相同的展示字段 | 配色是设计选择，不向网关增加字段；未读与搜索仍待定 |

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
| 01 | 会话未读数、最近消息摘要聚合、排序、搜索、相对时间展示规则。**已拍板（2026-11-10）**：`Conversation` 不加未读数/最近消息摘要/搜索，这些走 Matrix；排序可用已有 `last_activity_at`，见 `gateway/docs/contract.md` §8.4（B11） | 01、配色稿 |
| 02 | AI 好友角标如何从多成员会话关系汇总；群头像/成员排列与群名称生成规则。**已拍板（2026-11-10）**：角标只在 `kind=ai` 且该查看者视角下 `relation.state=friend` 时出现，不做跨群汇总，见 `gateway/docs/contract.md` §2.3 / §8.4（B6）；群头像/群名规则仍待定（归 C） | 01、02 |
| 03 | 人类消息发送、输入框交互与 Matrix 接入边界；纯人类群登记流程。`CreateConversationRequest.persona_ids` 至少一项，与纯人类群展示有接入缺口（网关侧口径见 `gateway/docs/contract.md` §2.2：只登记含 persona 的会话，纯人类房间由 App 直接对 Matrix 展示；界面与 Matrix 登记流程仍待定） | 02 |
| 04 | 多 AI 群轮流发言调度、输入/在线状态、消息编辑/撤回界面。**已拍板（2026-11-10）**：v1 不加 `/conversations/{id}/turn`，调度作为网关内部状态，发言仍走 `POST /policy/evaluate` + `POST /messages`，见 `gateway/docs/contract.md` §8.4（B3）；输入/在线状态、编辑撤回仍归 C/Matrix 待定 | 02 |
| 05 | 人类个人资料、账号显示、头像来源和独立签名字段 | 06 |
| 06 | `@ai_xiaoyu`、`@ai_laoshi` 短别名到 persona / Matrix 身份的映射；本稿只作虚构展示文案。**已拍板（2026-11-10）**：v1 不加 alias API，规范身份是 `persona_id` / Matrix user id，`@ai_*` 仅作 UI 展示、mention 仍提交规范 ID，见 `gateway/docs/contract.md` §8.4（B7） | 02、06、07、流程一、配色稿 |
| 07 | AI 独立签名字段；本稿把 `bio` 显示为签名的映射是否采纳。**已拍板（2026-11-10）**：继续用 `Persona.bio` 作为签名展示，不新增 `signature` 字段，见 `gateway/docs/contract.md` §8.4（B5） | 07 |
| 08 | 多会话关系快照如何汇总到 AI 资料；资料编辑权限及入口、好友审批接口。**已拍板（2026-11-10）**：资料页只用当前查看者与该 AI 的单聊 `Conversation.relation`（无单聊显示 `stranger`）；编辑/审批走 B2.8 新增的 `/relations/*`（动作接口细节未定义），见 `gateway/docs/contract.md` §2.3 / §8.4（B6、B2）；资料编辑入口界面仍归 C | 07、通讯录 |
| 09 | 好友候选列表及搜索接口、名单选择体验 | 04、通讯录 |
| 10 | 媒体选择/上传到 `mxc_uri` 的衔接、草稿持久化、发布失败后保留编辑内容的策略 | 04、流程一 |
| 11 | 动态详情错误提示、评论回复/点赞的加载、重试及撤销交互 | 03、流程一 |
| 12 | 发帖触发 AI 的订阅、接收回执、调度与是否主动评论的策略。**已拍板（2026-11-10）**：发帖默认**不**自动触发 AI 评论，所有者开启后才触发，且计入 `proactive.max_per_day` 与防刷屏（`anti_spam` / `ai_loop`），见 `gateway/docs/contract.md` §8.4（B8）；订阅对象与回执形状仍未定，不写接口 | 流程一 |
| 13 | AI 内部评论/点赞具体内部接口（内部写入边界已定；公开路径口径见 `gateway/docs/contract.md` §2.0 与 `gateway/docs/moments.md` §3，具体内部接口未定） | 流程一 |
| 14 | 评论事件到通知卡片的创建时机、来源能力标识与关联字段；具体动态/评论跳转路由与推送打开处理。**已拍板（2026-11-10）**：`source.capability_id` 用 `social.moments`（`source.module=social`），不发明新通知对象，见 `gateway/docs/contract.md` §3.7 / §8.4（B10）；跳转路由与推送打开仍归 C/D | 05、流程一 |
| 15 | 通知未读数、分类、排序、折叠摘要、状态呈现与通知偏好界面；不存在可直接引用的独立通知对象 | 05 |
| 16 | 授权业务 `payload`、模块结果 `result` 结构、操作真实影响/回滚范围；演示卡片仅为虚构场景 | 05、流程二、流程三 |
| 17 | 设备密钥注册、签名输入的字节编码/序列化及 `assertion` 编码；Face ID 不可用时的产品处理（不得自行降级绕过） | 流程三 |
| 18 | 取消、认证失败、过期、版本冲突、重复处理与网络结果未知时的具体文案/界面；安全边界和幂等规则已定 | 流程二、流程三 |
| 19 | manifest 到“发现”分组与排序、底部导航位置/显示策略的正式约定；清单变化时的界面更新与缓存体验 | 08 |
| 20 | 朋友圈、角色、桥接的能力 ID / 入口映射，以及小镇启用后的路由；全模块目标目前不可当成真实清单。**已拍板（2026-11-10）**：保留能力 ID `social.moments`（`module=social`）/ `role.personas`（`module=role`）/ `bridge.manage`（`module=bridge`），发布前仍可改、不写入示例清单，见 `gateway/docs/contract.md` §3.7 / §8.4（B9）；小镇启用后的路由仍待 M4（B4） | 08 |
| 21 | 字号缩放、无障碍读屏标识、交互热区、键盘弹出等实现与适配验收 | 全部；静态稿只验证所示尺寸 |

与 D 线相关的待定项已于 2026-11-10 由所有者拍板，定论见 [notify/docs/contract-gaps.md](../../notify/docs/contract-gaps.md)（B1–B10）与 [notify/docs/data-model.md](../../notify/docs/data-model.md)：14→§2.8，15→§2.7（未读/折叠/分类/排序/偏好；状态呈现仍随 C 线），16→§2.6（`payload`/`result` 不设全局 schema；操作影响与回滚属业务模块），17→§2.6（生物识别 fail-closed；断言编码与设备密钥注册为已定方向、待 D2.1 PR），20 的桥接能力 ID→§4.2（`bridge.feishu` / `bridge.telegram` / `bridge.qq`）。朋友圈、角色、小镇入口仍由相应线定稿，不在上述定论内。

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

导出需要 Node.js 22+（使用内建 WebSocket），不安装依赖。每个 HTML 使用独立的临时 Chrome 配置，临时目录及 `TMPDIR` 均限制在 `docs/design/`，结束自动清理。导出只读取本地静态页面，禁用外部 HTTP/HTTPS 资源请求，不复用日常浏览器配置。

脚本按画框数设定精确窗口宽度：`64 + 390 × 画框数 + 24 × (画框数 − 1)`，测量完整页面内容高度后调整窗口高度并截图；像素密度为 1。标题、所有画框、画框外注释及页尾均纳入 PNG，不再使用固定 1500 高度。导出同时检查画框尺寸、内容溢出、标题居中、四个 Tab 和 SVG，结果写入 [qa-report.json](qa-report.json)。重复执行覆盖同名 PNG；检查失败不覆盖该页既有截图。

### 文件清单

- 页面：`01-chat-list.html`、`02-chat.html`、`03-moments-feed.html`、`04-compose.html`、`05-notifications-cards.html`、`06-profile.html`、`07-ai-profile.html`、`08-dynamic-entries.html`。
- 流程：`flow-1-moment-ai-comment.html`、`flow-2-low-risk-approve.html`、`flow-3-high-risk-faceid.html`。
- 最终规范：[palette-final.html](palette-final.html)。两份旧配色稿及对应 PNG 已移除。
- 维护与导出：[design-system.css](design-system.css)、[apply-theme.py](apply-theme.py)、[export.sh](export.sh)、[export.mjs](export.mjs)、[qa-report.json](qa-report.json)、本 README。

`python3 docs/design/apply-theme.py` 将统一样式和自绘 SVG 内联进所有 HTML，随后再运行导出。交付 HTML 可独立打开，无脚本、外部字体、外部图片或联网依赖。

### png/ 文件清单

| 文件 | 尺寸 |
|---|---|
| [01-chat-list.png](png/01-chat-list.png) | 454 × 1281 |
| [02-chat.png](png/02-chat.png) | 1282 × 1241 |
| [03-moments-feed.png](png/03-moments-feed.png) | 868 × 1259 |
| [04-compose.png](png/04-compose.png) | 868 × 1222 |
| [05-notifications-cards.png](png/05-notifications-cards.png) | 1282 × 1241 |
| [06-profile.png](png/06-profile.png) | 454 × 1263 |
| [07-ai-profile.png](png/07-ai-profile.png) | 454 × 1281 |
| [08-dynamic-entries.png](png/08-dynamic-entries.png) | 1282 × 1241 |
| [flow-1-moment-ai-comment.png](png/flow-1-moment-ai-comment.png) | 1696 × 1222 |
| [flow-2-low-risk-approve.png](png/flow-2-low-risk-approve.png) | 1282 × 1241 |
| [flow-3-high-risk-faceid.png](png/flow-3-high-risk-faceid.png) | 1696 × 1241 |
| [palette-final.png](png/palette-final.png) | 868 × 1222 |

## 验证范围

本次验收针对静态交付：HTML 独立性、画框尺寸与溢出、PNG 覆盖及尺寸、字段对应和截图视觉抽查。真实网络、通知投递、Face ID 及后端联调不在本次文档任务中。此说明不将静态演示视为线上验收。

本次静态验证已完成：12 个 HTML、29 个 390 × 844 画框通过尺寸、横纵向溢出、子元素边界、标题居中、四个 Tab 与 SVG 检查；12 张 PNG 的实际尺寸与上表一致，页尾和画框外说明完整。已逐张查看全部 PNG。A/B 画框未泄露对方姓名或互动；所有色值和 24 种 SVG 语义均已记录，未引用外部资源。待定项清单与既有页面/流程契约映射逐字保留，仅将旧配色文件记录更新为最终规范。只修改 docs/design/，未运行 git 命令；真实功能与接口未在本轮执行。
