# 公网接入方式与网关路径（#128 决策记录）

| 项 | 内容 |
|---|---|
| 任务 | #128 公网接入（Cloudflare Tunnel / frp / IPv6+DDNS + 管理页配对二维码） |
| 状态 | **仅决策记录**：edge（Caddy）、接入模块、向导、管理页均未实现，见 #128 PR 拆分 |
| 相关 | [pairing-qr.md](../contracts/pairing-qr.md)（§3.3.1 fp 场景、§11 B1–B6）、[paired-federation.md](paired-federation.md)、[push-sygnal.md](push-sygnal.md) |

所有域名一律用 `example.org` 占位；本文不含任何真实域名、令牌或证书。

## 1. 已定决策

| 来源 | 决策 | 依赖 |
|---|---|---|
| 总管 2026-11-05 | frp：TLS 在本机（部署宿主）终止，云服务器只做 TCP 透传 | #128 PR-d |
| 总管 2026-11-05 | DDNS：首版 Cloudflare + 阿里云，提供商接口可扩展 | #128 PR-e |
| 总管 2026-11-05 | 管理页：Matrix 管理员账号登录；另有本机 CLI 一次性管理 token（初始化/应急） | #128 PR-f |
| 总管 2026-11-05 | `push.<domain>` 默认不对公网开放（`BOTVILLE_PUSH_PUBLIC=false`），留开关 | push-sygnal.md |
| 总管 2026-11-05 | 保留局域网选项，标注「仅测试，外网无法连接」 | #128 向导 |
| 棉棉霸 2026-11-10（B1） | 网关对外暴露在**主域路径** `https://<domain>/_botville/gateway`，不另开 `gw.<domain>` | B 线网关、C 线客户端、#128 edge |
| 棉棉霸 2026-11-10（B2） | App→网关鉴权：网关校验 Matrix OpenID token（#124 已落地；App 只发 `MatrixOpenID`，网关为其他调用方保留 `matrixUserToken`，见 §3） | B 线网关（#124）、C 线客户端 |
| 棉棉霸 2026-11-10（B5） | 有自有域名时先上 **Cloudflare Tunnel**，**裸域**当 `server_name`；国内不稳再补 frp | #128 PR-c / PR-d |

## 2. B1：网关路径

- edge（Caddy）把 `https://<domain>/_botville/gateway/*` 去掉前缀后反代到网关
  容器；网关 API 在其后的 `/v1/…`（`gateway/openapi/gateway.v1.yaml` 的 `servers`
  为 `https://{host}/v1`，部署时 host 部分即 `<domain>/_botville/gateway`）。
- 管理页签发 App 配对二维码时写 `gw=https://<domain>/_botville/gateway`（不带
  `/v1`，pairing-qr.md §3.2）。
- 与 `hs` 同源：一个隧道主机名、一张证书；有 `fp` 的部署（frp、局域网）网关请求
  按 pairing-qr.md §3.3.2 复用 pin。
- 依赖：B 线网关须支持部署在路径前缀之后（生成的绝对链接、重定向不得丢前缀）；
  C 线按 §3.2 拼 `/v1`（现状已支持）；#128 edge 增加该路由。

## 3. B2：OpenID token 鉴权

- App 用 Matrix 标准接口 `POST /_matrix/client/v3/user/{userId}/openid/request_token`
  取得短时 OpenID token，随网关请求提交（请求头格式由网关契约定义，#124）。
- 网关用 `GET /_matrix/federation/v1/openid/userinfo?access_token=…` 向**本部署**
  homeserver 换回 `sub`（Matrix 用户 ID）。该调用走 **compose 内网直连 homeserver**，
  **不经** fedgate（fedgate 对 `/_matrix/federation/*` 要求 X-Matrix 签名，会拒绝此
  请求），也不走公网。homeserver 须在内网监听上提供 openid 资源。
- 目标：网关只拿短时 OpenID token，不拿用户的 access token；OpenID token 不得写入日志。
- **与网关契约的关系（已对齐）**：OpenID 校验已在 #124 落地，契约见
  `gateway/docs/contract.md` §1.2.3。**App 只发 `Authorization: MatrixOpenID <token>`**，
  无 feature flag、无回退，**不得**拿用户的 Matrix access token 当 `Bearer` 兜底。网关在
  `/moments*` 上继续接受 `Authorization: Bearer <Matrix access token>`
  （`matrixUserToken`→whoami），那是留给 **App 以外其他调用方**的阶段 1 共存路径，未标
  `deprecated`、仍有效。退场节奏（T1 观察 → T2 清点 → T3 进阶段 2 标 `deprecated` →
  T4 进阶段 3 移除）见契约 §1.2.3；其中**阶段 2（标 `deprecated`）与阶段 3（移除
  `matrixUserToken`）须先公告、先报总管**，阶段 1（现状，Bearer 仍有效）不受此约束。
- 依赖：B 线网关契约与实现（#124）、C 线客户端、A 线 compose（内网可达与 listener
  配置，实现时验证）。

## 4. B5：接入方式与 server_name

- 推荐顺序：有自有域名 → **Cloudflare Tunnel**（主机不开入站端口）；国内访问不稳时
  再补 **frp**（TLS 在本机终止）；IPv6+DDNS 作为有稳定公网 IPv6 时的备选；局域网仅测试。
- **`server_name` 用裸域**（`example.org`，用户 ID 形如 `@alice:example.org`）；
  `/.well-known/matrix/server` 委派到 `example.org:443`，Tunnel 模式不开 8448。
  `server_name` 一旦确定**不可更改**，向导须在首次部署时强提醒。
- 二维码 `fp`：Cloudflare Tunnel / 公网 CA 证书不带；frp（本机自签/私有 CA）与
  局域网必须带（pairing-qr.md §3.3.1）。
- 已知风险：Cloudflare 免费版单次上传 100MB 上限；Cloudflare 对 X-Matrix
  `Authorization` 头的透传需在 #128 PR-c 做 e2e 验证。
- 依赖：#128（PR-b edge、PR-c Cloudflare 模块与向导、PR-d frp）。具体部署机的
  域名与接入配置只写在部署侧，不进仓库。
