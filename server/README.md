# server/ — 服务端与部署（A 线，ZCode）

- Matrix 服务器部署：Synapse + Postgres，或 Tuwunel / continuwuity；Docker Compose profile 按模块启用
- 只依赖标准 Client-Server、Application Service、Push Gateway 接口；CI 双服务器测试
- Sygnal 推送网关 + APNs；appservice 注册 AI 账号命名空间
- 仅配对联邦与扫码配对后端（可单方面切断）
- 动态存储与可见范围、AI 关系存储与多 AI 群
- 小镇状态服务（位置与状态同步、降频/关闭）、素材存储
- 安装向导、升级与导出脚本
