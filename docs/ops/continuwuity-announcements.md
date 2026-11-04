# continuwuity：关闭 announcements 检查（issue #91 / #109）

## 为什么关掉

continuwuity（conduwuit 系）启动后，`announcements` worker 会请求
`https://continuwuity.org` 拉取上游公告。该请求**不可被中断**：若 SIGTERM
落在请求飞行期间，服务管理器会卡在 `Waiting for service manager...`，直到
`stop_grace_period`（compose 里 10m）到期后被 SIGKILL（exit 137）。

本地慢网络下窗口约数十秒（connect/read 超时叠加）；GitHub Actions 等快网络
上请求常 <1s 结束，因此「smoke 后立刻 stop」的计时回归**可能假绿**——即使
去掉 `CONTINUWUITY_ALLOW_ANNOUNCEMENTS_CHECK=false` 也容易立刻退出。CI 因此
另用 `docker inspect` 断言该环境变量为 `false`（见 `.github/workflows/ci.yml`
Graceful stop 步，#109）。

botville 默认关联邦、也不需要未请求的外连，故在
`server/compose/docker-compose.yml` 的 continuwuity 服务上设置：

```yaml
CONTINUWUITY_ALLOW_ANNOUNCEMENTS_CHECK: "false"
```

## 会失去什么

- admin room 里的上游公告 / 版本提示（若有）不再自动拉取。
- 其它功能（CS API、注册、推送 denylist 等）不受影响。

## 何时可回收

上游修复 announcements worker 的可中断 shutdown（或默认不在停机路径上阻塞）
之后，可删掉该环境变量，并考虑向上游跟进 issue。回收前应用慢 DNS / 黑洞
路由复现「去掉修复 → Graceful stop 变红」再合入。

## 相关

- issue #91（根因）、#109（CI 防假绿 + 本文档）
- PR #106
