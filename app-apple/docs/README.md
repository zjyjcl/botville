<!-- SPDX-License-Identifier: Apache-2.0 -->

# 苹果端验证文档

- [DEBUG 真机推送](push-device.md)：APNs 权限与 token、Matrix pusher 注册/核对、Release 隔离和后续 iPhone 验收。
- [扫码配对](pairing-qr.md)：v1 二维码解析、兑换、注册/登录、SPKI 固定、mock 与 SDK 限制。
- [C2.5 动态界面（mock）](dynamic-ui.md)：功能清单、通用卡片、模块网页 session cookie 与前向兼容策略（2026-11-04，#38）。
- [C1.1 Element 验证与问题收集](c1.1-element-findings.md)：本地 Continuwuity + Element Web 实测、证据边界及 C2.x / C3.1 待办（2026-11-04，#33）。
- [C2.1 安全加固与验证边界](security-hardening.md)：Release HTTPS、Debug ATS、推送 URL 与错误分类。
- [会话安全与 soft logout](session-security.md)：固定 SDK refresh 能力评估及保留设备数据的密码重新认证。
- [加密策略（C2.6）](encryption-policy.md)：默认选择、二次确认、AI 命名空间配置、状态标识及设备验证/备份提示。
- [拉群与群管理（C2.7）](group-rooms.md)：owner 群列表与设置、多 AI 拉群、Matrix 聊天信息、错误与契约缺口。
- [视图测试结构](view-testing.md)：Core view state、类型化接线守卫、渲染层边界与后续方案。
