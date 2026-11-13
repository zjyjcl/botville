<!-- SPDX-License-Identifier: Apache-2.0 -->

# 视图测试结构

现有 `BotvilleCoreTests` 只依赖 `BotvilleCore`。本轮沿用此 target，不增加第三方依赖、
target、scheme 或构建配置，也不改签名。纯展示组件随本地 Swift Package 的 Core 自动纳入
Xcode App，无需修改 pbxproj 的文件成员。

## 约定与接线

业务数据 → Core 纯值 view state → SwiftUI 展示组件。View 只读 view state，不能另写一套
群开关、邀请人是否为空或私有心情标记的条件；事件通过闭包回传，网络调用仍由 App/控制器负责。
加载、错误、筛选等容器状态仍由原有 observable model 管理，本次不重写整个页面。

| 场景 | 唯一决策入口 | 消费者与验证 |
| --- | --- | --- |
| 主人/对方心情 | `PersonaMoodPresentation(ownerState:)` / `(counterpartMood:)` | 两个页面调用既有入口；Core `PersonaMoodBadge` 唯一输入为 presentation；`MoodViewStateTests` 覆盖所有心情、公开/私有/nil、未知解码 |
| 群房间行 | `GroupRoomRowViewState` | Core `GroupRoomRow` 通过 `ForEach(state.actions)` + switch 渲染；`ConversationViewStateTests` 覆盖开关 × accepted/encrypted_disabled/left/rejected_encrypted/unknown × muted/mention_only/unknown × 提交中，以及设置未确认门控 |
| 群列表关闭提示 | `GroupRoomListViewState` | `PersonaGroupRoomsView` 只按可选 notice 展示；测试开关对应的提示文字 |
| 新建会话/发起群聊 | `NewConversationFormState` | `NewConversationSheet` 使用标题、字段文案、解析后的 invitees/draft；Core `NewConversationCreateButton` 读取 `isCreateDisabled`；测试空/非空 × 私聊/群聊 × 创建中 |

群动作顺序由 state 固定：可管理房间先模式控制/状态，再退出。关闭群功能或设置未确认时，
mention_only 只提供「设为安静」，muted 只显示「已设为安静」；都保留退出。
开启且设置已确认时提供双向 Toggle；未知/终态保持原有只读行为。
提交中保持动作可见并禁用。日期格式化保留在展示组件，以保持系统区域设置行为。

邀请人解析复用 `GroupInviteSelection`，保留排序、去重、逗号/空白拆分；私聊忽略隐藏的群多选。
同一个 state 的 invitees 组成实际提交 draft，避免按钮和提交名单采用不同来源。
非空但无效 MXID、私聊多人仍在提交时由 `RoomCreationDraft.validate()` 报错，不提前改变按钮行为。

## 轻量接线守卫与局限

`testPresentationOnlyViewsAcceptTypedStateAndRetainIt` 用明确函数类型引用三个实际 SwiftUI
组件的初始化入口，并实例化后核对保留的输入。组件没有原始 room、groupsEnabled、
invitees 或 moodPublic 等输入；相关决策只能来自指定 state。App 的四种平台/配置构建
检查真实调用点能否编译。没有读源码字符串的测试。

这比只测独立 helper 更能约束组件接口，但不是完整接线或渲染证明：无法防止父页面未来
传错 state、移除组件或在组件 body 新增错误逻辑，也未断言实际按钮树、禁用交互、布局、
颜色、系统可访问性树或点击回调。现有文案、控件类型和样式保持不变。

本轮不采用 NSHostingView 可访问性树断言：未挂载窗口的 SwiftUI 子树不适合作为稳定的
跨 macOS/CI 契约，挂载又引入窗口、主线程生命周期及运行环境依赖。
采用稳定的类型化初始化守卫，不声称已完成渲染层断言。

## 后续真正视图/UI 测试的备选方案（未实施）

1. **Package.swift 新增依赖 BotvilleApp 的 testTarget**：通过 `@testable import BotvilleApp`
   直接测试可执行 target 中的页面，减少为了测试把展示代码放入 Core 的需要。
   需验证当前 SwiftPM 工具链对 executable target 的测试导入，以及 App 入口/平台条件；
   私有页面还需调整可见性。只导入或实例化仍不等于渲染测试，需另选稳定 hosting/交互断言。
   代价是新增 target、构建面及 App 初始化隔离，故本轮硬约束下不实施。
2. **Xcode UI test target / ViewInspector**：UI test 可走真实页面、点击和系统可访问性，
   证明禁用状态与用户路径，但需要 scheme、模拟器、启动夹具和较慢且更易波动的 CI；
   ViewInspector 可快速检查 SwiftUI 结构及交互，但引入第三方依赖，受 SwiftUI/库版本适配影响，
   也不等于系统渲染或可访问性验收。两者分别违反本轮不新增 target/不加依赖的边界，均只保留方案。
