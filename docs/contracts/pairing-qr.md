# App 扫码配对契约（Pairing QR v1，#128 PR-a）

| 项 | 内容 |
|---|---|
| 任务 | #128 PR-a —— App 扫码配对**仅契约**（不含服务端实现与 App UI） |
| 机器可读规范 | [`pairing-redeem.openapi.yaml`](pairing-redeem.openapi.yaml)（冲突以它为准） |
| 示例 | [`examples/pairing-qr/`](examples/pairing-qr/) |
| 参考实现 | `server/pairing_qr_contract.py`（纯标准库，解析与校验） |
| 测试 | `server/tests/test_pairing_qr_contract.py`（无 Docker） |
| 相关 | [`docs/deploy/paired-federation.md`](../deploy/paired-federation.md)（服务器间配对，见 §1）、[`gateway/docs/contract.md`](../../gateway/docs/contract.md)（功能清单） |

本文是人类可读说明；与 OpenAPI 文件冲突时以
[`pairing-redeem.openapi.yaml`](pairing-redeem.openapi.yaml) 为准，并视为本文的 bug。
所有示例域名均为占位（`example.org` / `hs.example.org` / `gw.example.org`），示例
token 一律是 `<redacted-example>` 这类明显假值；**任何真实令牌、密钥、密码不得进入
仓库、PR、issue、日志或截图**（见 `CONTRIBUTING.md`）。

---

## 1. 与"仅配对联邦"的关系（必须区分）

botville 已有一套二维码配对：A2.1 的 `botville-pair:v1:…`（见
[paired-federation.md](../deploy/paired-federation.md)）。那是**服务器之间**建立联邦的
一次性邀请码，与本契约的 App 扫码配对**完全分开**：载荷格式、兑换端点、错误码、
生命周期互不通用，实现上也不共享端点。

| 维度 | 本契约：`botville://pair?v=1` | A2.1：`botville-pair:v1:…` |
|---|---|---|
| 参与方 | 用户的手机 App ↔ 本部署 | 两个 botville 部署（管理员操作） |
| 目的 | 兑换注册/登录令牌，完成 App 首次配对 | 建立联邦互通（invite → redeem → accept） |
| 载荷 | hs/gw 的 https URL + 一次性 code | server_name + api_url + invite_id + secret |
| 兑换端点 | `POST {hs}/_botville/pair/redeem` | `POST {api}/v1/pairing/redeem` |
| 错误码 | `BV_PAIR_*`（§4.4） | pairing 服务自有错误集 |
| 有效期 | 签发后 5 分钟 | `PAIRING_INVITE_TTL`（默认 600s） |

两套并存时的识别规则：载荷以 `botville://pair?` 开头的是 **App 配对**；以
`botville-pair:v1:` 开头的是**联邦邀请**。App 遇到后者应提示"请在服务器管理页使用"，
不得尝试解析或兑换。

## 2. 流程概览

```
管理员（管理页 / 本机 CLI）            App（C2.2）
  │ 签发：32 字节随机 code              │
  │   库里只存 sha256(code)，TTL 300s    │
  │   记录保存签发时的 hs(/gw)(/fp)      │
  │                                     │
  │  ←—— 线下出示二维码（URI 文本）——→   │ 扫码得到 URI
  │                                     │ ① 解析 + 本地校验（§3，整体拒绝）
  │                                     │ ② POST {hs}/_botville/pair/redeem
  │                       ┌─────────────┤    {code, device_display_name}
  │                       │ 兑换端点      │ ③ 200：校验 homeserver 与 URI 逐字符一致
  │                       │ 限流(IP+全局) │    （§5.2，不一致即拒绝）
  │                       │ 事务内原子核销 │ ④ 用 auth.token 走 Matrix 注册/登录
  │                       │ 返回 auth 令牌 │ ⑤ GET features_url 拉取功能清单
```

- 兑换端点挂在**二维码中 hs 域名**下（`{hs}/_botville/pair/redeem`）；部署上可由
  反向代理转发到配对后端（同 A2.1 的 `/_matrix/pairing` 转发方式），契约只规定 App
  可见的地址 = `hs` + 固定路径。
- App 的本地校验（§3）在发任何网络请求**之前**完成：格式不合法的二维码绝不产生
  兑换请求。

## 3. 二维码 URI 规范

```
botville://pair?v=1&hs=<https-url>[&gw=<https-url>][&fp=<spki-hash>]&code=<b64url-43>&exp=<unix-sec>
```

### 3.1 参数表

| 参数 | 必填 | 格式 | 说明 |
|---|---|---|---|
| `v` | ✅ | 固定字符串 `"1"` | 契约版本。未知值（`2`、`01`、`1.0`…）整体拒绝；旧 App 遇新 `v` 应提示升级 |
| `hs` | ✅ | `https://` 绝对 URL（§3.2） | homeserver 基址；兑换端点由它拼出 |
| `gw` | ❌ | `https://` 绝对 URL（§3.2） | AI 网关基址；无网关部署省略（见 §5.1.1「无网关时的行为」）。出现时仍须通过 §3.2 全部校验，空值（`gw=`）按格式错误拒绝 |
| `fp` | ❌ | `sha256:<base64url-43>`（§3.3） | 叶证 SPKI sha256；有则按 §3.3.2（pin **替代**系统根 + 自锚 SAN/有效期/EKU；作用域 `(scheme,host,port)`）；**仅**自签或私有 CA 部署提供，公网 CA 省略 |
| `code` | ✅ | 43 字符 base64url 无填充 | 32 字节随机数（§4.2） |
| `exp` | ✅ | 十进制 Unix 秒 | code 过期时刻 = 签发时刻 + 300s |

### 3.2 hs / gw 的 URL 规则

只接受 `https://` 绝对 URL，且：

- **小写 `https://` 前缀**（规范形式；`HTTPS://`、`http://`、无 scheme 一律拒绝）；
- **无 userinfo**（`https://user:pass@hs.example.org/` 拒绝）；
- **无 query**（`…?x=1` 拒绝）、**无 fragment**（`…#f` 拒绝）；
- host 必填（`https:///` 拒绝）；端口可选但必须合法（`…:99999` 拒绝）；路径可空可有。

### 3.3 fp（证书指纹）

- 值 = `"sha256:" + base64url(sha256(SPKI(叶证书)))`，**无填充**，摘要段恰好 43 字符
  （解码 32 字节）；base64url 必须是规范编码（解码后重新编码与原文一致，尾部残留位
  非零即拒绝）。pin **只认叶证 SPKI**，不接受中间证/根证指纹。
- 语义：有 `fp` 时，它是 App 对 **homeserver 源**（`(scheme, host, port)`，见 §3.3.2）
  HTTPS 的 **pin 锚点**：**替代**系统信任链（**不得**再要求系统根评估通过），但
  **不等于**「只比 SPKI、其它都不查」——还必须做叶证自锚下的 SAN / 有效期 /
  serverAuth EKU 校验（完整规则见 §3.3.2）。**仅自签或私有 CA 部署提供**；公网 CA
  （如 Let's Encrypt）部署省略该参数——证书轮换会让指纹失效。无 `fp` 时的信任见
  §3.3.2。

### 3.3.1 何时必须带 fp（定稿：fp 可选）

`fp` 是可选参数（总管拍板；C 线 #130 已按可选合并）。原因：Cloudflare Tunnel 与公网 CA 证书会轮换，无法 pin。信任语义（有/无 fp）定稿见 §3.3.2 与 #132。

| 部署场景 | fp | 说明 |
|---|---|---|
| 自签证书 | **必须带** | 有 fp 时按 §3.3.2：SPKI pin **+** 叶证自锚（SAN/有效期/serverAuth EKU）；不要求系统根信任 |
| frp（TLS 在 8845 端终止，云服务器仅 TCP 透传） | **必须带** | 证书由本机签发/持有时按自签处理；若本机用公网 CA（ACME）证书，则按“公网 CA”行处理 |
| 局域网（仅测试，外网无法连接） | **必须带** | 局域网通常无公网 CA 证书；有 fp 时按 §3.3.2 |
| 公网 CA 证书（如 IPv6+DDNS 用 ACME 证书） | 可以不带 | 证书轮换会使 pin 失效；不带则仅系统信任 |
| Cloudflare Tunnel | 可以不带 | 边缘证书由 Cloudflare 管理并轮换；不带则仅系统信任 |

- **不带 fp 时，App 只依赖系统信任链**（操作系统根证书 + 常规主机名校验），不做额外锁定。
- 签发端（管理页/向导）应按当前接入方式自动决定是否写入 fp；“必须带”的场景缺 fp 时签发端拒绝签发（服务端签发约束，URI 解析层仍视 fp 为可选）。

### 3.3.2 信任语义（有 fp / 无 fp）（#132 定稿）

棉棉霸 2026-11-06 拍板。**不采用**「pin 或系统信任任一通过」。

**源（origin）定义**：`(scheme, host, port)`。`scheme` 必须是 `https`；`host` 的 DNS
比较**大小写不敏感**（与 §5.2「`homeserver` URL **字符串**逐字符、大小写敏感」是
两个层面，不得把 §5.2 做成大小写折叠）；缺省端口按惯例为 **443**（显式 `:443` 与
缺省端口视为同一 port）。

| 情况 | App 对 **homeserver 源** HTTPS 的信任 |
|---|---|
| **有 `fp`** | **不以系统根为准**（**不得**再要求完整系统信任链 / 默认 `SecTrustEvaluateWithError` 通过），但**也不是**「只比 SPKI」。必须同时满足下列全部条件，否则拒绝连接（App 端证书错误，见下；**非** §4.4 的 `BV_PAIR_*`）：① **叶证** SPKI SHA-256 与 `fp` **常量时间**比较匹配（只认叶证，不接受中间/根 pin）；② 以该叶证为**唯一锚点**（自锚：如 `SecTrustSetAnchorCertificates` + 仅锚点 / 等价）评估；③ **SAN** 匹配目标主机——用 `dNSName`（DNS）或 `iPAddress`（字面 IP）；**拒绝 CN 回退**；目标为 IP 时必须出现在 `iPAddress`；④ **`notBefore ≤ now ≤ notAfter`**；⑤ 含 **serverAuth** EKU（Extended Key Usage）。长期自签（有效期 >398 天）在自锚模式下**可接受**。拒绝原因应可区分「指纹不符 / 主机名不符 / 证书过期或未生效 / EKU 不符」等，且**不**把证书 PEM/DER 写入日志。 |
| **无 `fp`** | **仅**系统信任（OS 根 + 常规 hostname / 有效期 / EKU），**不做** SPKI pin。 |

**何时套用 pin（作用域）**：

- pin **只**作用于二维码 `hs` 解析出的源 `(scheme, host, port)`，且仅在**专用**
  pairing / 已配对会话的 HTTPS 客户端上（URLSession + TLS delegate 等）；
  **不得**全局安装 TLS delegate、**不得**全局关闭校验、**不得**改写其它 host 的
  TLS 行为。
- 对一次 TLS challenge：若 challenge 的 `(scheme, host, port)` 与 `hs` 源**一致**
  → 按上表「有 `fp`」规则评估；若**不一致** → **不套用**该 pin，走系统默认信任
  （如 `.performDefaultHandling`），**不**因 QR 带了 `fp` 而拒绝该其它源。
- **`gw` 复用 pin 的条件**：仅当 `gw` 与 `hs` 的 `(scheme, host, port)` **完全相同**
  时，同 host 网关 API（如 `HTTPGatewayClient`）才复用同一 pin；否则网关走系统
  TLS，**不得**因 QR 的 `fp` 拒绝。
- **覆盖（在作用域内、且由 App 自管 URLSession 发起时）**：兑换
  `/_botville/pair/redeem`；同源上的注册/登录 HTTPS（**仅当**不走 matrix-rust-sdk
  内部栈时，见 §3.3.3）；同源网关 API。
- **不覆盖**：跨源 `gw`；WebKit 模块页；跨 origin 重定向（仍禁止把 code/令牌送走）；
  SDK 内部请求（§3.3.3）。

**信任来源与缓解**（有 `fp` 时）：`fp` 来自当面扫码的二维码传递通道（**不是**
「首次使用即信任 / TOFU」——不得实现成「首连记录 pin、之后只比对缓存」而跳过
确认页）。缓解依赖：配对码一次性 + 短过期、二维码传递通道、确认页展示 `hs` 与
指纹摘要；用户确认后才兑换。

**证书轮换**：hs 换证后，若 SPKI 变了，旧二维码 / 已保存的 pin 会失败 → 须重新
签发配对码并重新配对（会话侧提示由 App 实现）。服务端换证时**建议保留同一密钥**
（只换证不换钥），以降低已配对客户端的断连面；密钥轮换则必须重新配对。

**与 #130 文档/实现的关系**：合入本契约前，`app-apple/docs/pairing-qr.md` 与
`PairingTLS.swift` 仍为「先系统信任再比 fp」的**旧语义**，将在 C 线 #132-PR2
对齐本节；在此之前以**本文件**为契约真源，旧 App 文档不得覆盖本节。

### 3.3.3 SDK 同步与 pin 边界（已知限制）

matrix-rust-sdk（当前绑定如 `matrix-rust-components-swift` 26.x）的同步、消息等
**内部** HTTPS **无法**做叶 SPKI pin 或注入自定义 TLS verifier / URLSession。
暴露的根证书接口（如 `addRootCertificates`）**不等于**叶 SPKI pin；**禁止**用
关闭 TLS 校验（如 `disableSslVerification`）代替 pin。

**配对 pin ≠ 全链路 pin。**

| 阶段 | 路径 | 能否按 `fp` pin？ |
|---|---|---|
| 扫码 → 确认页 | 本地解析 URI，无 HTTPS | N/A |
| redeem | App 自管 URLSession / TLS delegate | **能**（有 fp 时按 §3.3.2） |
| 同源注册/登录 HTTPS | **仅当**由 App 自管 URLSession 发起 | **能**；若实际走 matrix-rust-sdk 内部请求则 **不能**（落入下一行） |
| 同源网关 API | App 自管 URLSession（且 `gw` 与 `hs` 同源，见 §3.3.2） | **能** |
| **SDK sync / 消息等** | SDK 自带 TLS | **不能**；仍系统信任（或 SDK 自带策略） |
| WebKit 模块页 | 系统 TLS | **不能** |

**产品后果（自签 / 私有 CA）**：有 fp 时，作用域内的 App 自管 HTTPS 可通，但
**SDK 同步仍可能**因系统不信任叶证而失败。本契约**不**宣称 SDK 流量已 pin。
缓解属部署/App 产品决策（**不在本契约 PR 范围**），建议顺序：① 评估向 SDK
注入自签根（spike）；② 引导用户安装描述文件/根证书；③ 向上游提叶 SPKI pin /
自定义 verifier 需求。C 线须在 UI 呈现「配对 pin ≠ 全链路 pin」这一已知限制。

**责任边界**：A 线维护本契约与签发是否写入 `fp`（#128 接入方式）；C 线实现
§3.3.2（自锚 + SAN/有效期/EKU + `(scheme,host,port)` 作用域，#132-PR2）、确认页
展示、以及 SDK 无法 pin 时的用户提示。

### 3.4 编码与解析规则

- URI 整体是一个字符串（UTF-8），参数顺序任意；解析前允许去掉**首尾**空白。
- **不做 percent-decode**：规范值（URL、base64url、十进制秒）都不需要转义，出现
  `%` 即整体拒绝——避免同一值多种编码的歧义，也不把 `+` 当空格（不是
  `application/x-www-form-urlencoded`）。
- 解析步骤：去掉首尾空白 → 必须以 `botville://pair?` 开头 → 按 `&` 切分 → 每段按
  **第一个** `=` 切分键值 → 逐参数校验。

### 3.5 拒绝规则（fail-closed）

以下情况**整体拒绝**（App 不发起兑换，提示二维码无效；绝不部分接受）：

1. 任一参数**重复出现**（含大小写不同的同名参数——键名区分大小写，`V=` 是未知参数）；
2. **缺少任一必填**参数（`v`、`hs`、`code`、`exp`；`gw`/`fp` 可选，不算缺必填）；
3. `v` ≠ `"1"`；
4. 任一参数**格式错误**（§3.1–§3.3，含 base64url 长度/字符集/规范编码、exp 非规范
   十进制秒、URL 违反 §3.2）；
5. **未知额外参数**：拒绝（哪怕值是空的；不"忽略未知项"——防将来有语义的参数被旧
   App 静默丢弃）。当前不存在合法的可选参数除 `fp` 外的任何扩展；
6. URI 带 fragment、含 `%`、含空参数段（`&&`）、参数无 `=`、含内部空白/控制字符，
   或不以 `botville://pair?` 开头。

### 3.6 二维码里不放什么

- 二维码**只**承载 §3.1 的六个参数。`code` 是唯一凭证类字段，且是一次性、5 分钟
  过期、服务端只存 sha256 的短命值。
- **不得**出现：Matrix access token、任何 secret/密钥、密码、用户 ID（`@alice:…`）
  或房间 ID。`botville://pair?…&user=@alice:hs.example.org` 这类载荷按 §3.5-5 直接
  拒绝。

## 4. 签发与生命周期

### 4.1 签发（管理面）

签发与作废只经管理面（不对外匿名开放）：

- **管理员 Matrix 账号登录**管理页（Web）：输入/确认 `hs`、可选 `gw`（可选
  `fp`），服务端生成 `code` 并展示完整 URI / 二维码；
- 或**本机 CLI**（如 `botville-pairing admin issue`）持**一次性管理 token** 操作：
  token 生成后仅可用一次、短 TTL、仅监听 loopback。

签发动作：32 字节 CSPRNG 随机 → base64url 无填充得到 `code` → 库中记录
`sha256(code)`、`hs`（逐字符原样）、`gw`/`fp`（如有，逐字符原样）、
`exp = now + 300`、状态 `unused` → 返回 URI（`code` 明文**只在签发响应里出现
一次**，与 A2.1 invite secret 同规格）。

### 4.2 code 的属性

- 32 字节（256-bit）CSPRNG 随机数，base64url 无填充（43 字符）；
- 服务端**只存 `sha256(code)`**，兑换时以 sha256 后常量时间比对查找；
- 一次性；签发后 **5 分钟**过期（`exp` 为 Unix 秒）。

### 4.3 状态机与判定顺序

`unused → used | expired | revoked`（终态不可逆；重新配对须重新签发）。
兑换时按以下顺序判定，先命中先返回：

1. `used` → `409 BV_PAIR_USED`；
2. `revoked` → `410 BV_PAIR_REVOKED`；
3. `now ≥ exp` → `410 BV_PAIR_EXPIRED`；
4. 否则**单事务内原子核销**：读状态 → 复核未用未过期未作废 → 置 `used` → 签发
   auth token → 写审计 → 提交。并发兑换同一 code 只有一个成功（其余 409），
   无 TOCTOU。

### 4.4 错误模型

错误体统一 `{errcode, error[, retry_after_ms]}`（`error` 为可展示英文短句，App 自行
本地化）：

| errcode | HTTP | 触发 | 可重试 |
|---|---|---|---|
| `BV_PAIR_INVALID` | 400 | 请求结构非法（缺字段/未知字段/code 格式错）或**查无此 code**（不区分二者，防探测） | 否 |
| `BV_PAIR_USED` | 409 | code 已被兑换（重放） | 否 |
| `BV_PAIR_EXPIRED` | 410 | code 已过期 | 否（重新签发） |
| `BV_PAIR_REVOKED` | 410 | code 已被管理员作废 | 否（重新签发） |
| `BV_PAIR_RATE_LIMITED` | 429 | 触发任一层限流；**必带** `retry_after_ms`（毫秒，≥1，距最早可重试时刻） | 是，按 `retry_after_ms` |

### 4.5 限流（两层）

- **IP 层**：按客户端来源 IP 计数（建议默认 10 次/分钟，可配置）；
- **全局层**：整个部署的兑换请求计数（建议默认 60 次/分钟，可配置）；
- 任一层超限 → `429 BV_PAIR_RATE_LIMITED + retry_after_ms`（取触发层的最早可重试时刻）；
- 兑换处理的完整顺序：**① 限流（两层，纯计数，先于任何 code 查询）→ ② 请求结构
  校验 → ③ 按 `sha256(code)` 查记录（查无 → 400）→ ④ 状态判定（§4.3）→ ⑤ 事务内
  原子核销**。签名方无凭证可枚举：256-bit 随机 + 5 分钟窗口 + 双层限流。
- 反代后取真实 IP 的规则与 A2.1 pairing 相同：仅当直连地址 ∈ 受信代理网段时才采信
  `X-Forwarded-For`，否则用直连 IP——伪造 XFF 稀释不了限流桶。

### 4.6 审计

签发、兑换、作废、限流触发、非法请求均写审计事件（`kind` 分别为
`issued` / `redeemed` / `revoked` / `rate_limited` / `rejected`），字段含时间戳、
`kind`、记录 ID、`code_sha256`、来源 IP（兑换类）、操作者（管理类）、结果与原因。

审计**不记** `code` 明文与 auth token；`device_display_name` 记入兑换审计便于排查
滥用。日志与审计的保留期、裁剪策略沿用 A2.1 `store.prune()` 的做法。

## 5. 兑换接口

### 5.1 请求与响应

`POST {hs}/_botville/pair/redeem`（JSON，UTF-8；请求体 ≤ 64 KiB；服务端拒绝未知
字段，客户端**必须忽略**响应中未声明字段——前向兼容，同网关契约惯例）：

```json
// 请求
{
  "code": "QRPAIREXAMPLECODE0000000000000000000000000A",
  "device_display_name": "Alice 的 iPhone 17"
}
```

| 字段 | 必填 | 约束 |
|---|---|---|
| `code` | ✅ | 43 字符 base64url 无填充（与 URI 中相同） |
| `device_display_name` | ✅ | 1–128 个 Unicode 字符，App 生成的设备名 |

```json
// 200 成功
{
  "v": "1",
  "homeserver": "https://hs.example.org",
  "gateway": "https://gw.example.org",
  "auth": {
    "type": "registration_token",
    "token": "<redacted-example>",
    "expires_in_ms": 600000
  },
  "features_url": "https://gw.example.org/features.json"
}
```

| 字段 | 说明 |
|---|---|
| `v` | `"1"` |
| `homeserver` | **签发时保存的 hs 原样返回**（§5.2） |
| `gateway` | 签发时保存的 gw 原样返回；签发时无 gw 则为 `null`（字段仍必现，见下） |
| `auth.type` | `"registration_token"`（App 用 Matrix 注册流程换新账号）或 `"login_token"`（App 用 `m.login.token` 登录已有账号）；由签发时管理面决定，契约只传输 |
| `auth.token` | 一次性短时令牌；建议 `expires_in_ms` ≤ 600000（10 分钟）；过期未用须重新配对 |
| `features_url` | 功能清单（capability manifest）拉取地址，`https://` URL；其内容结构不在本契约（见网关契约） |

`registration_token` 与 `login_token` 在 homeserver 侧的具体核销（注册流程 / 登录
流程）由服务端实现负责，本契约只规定传输格式、短时性与一次性语义。

### 5.1.1 无网关时的行为

`gw` 是可选参数（§3.1）：无网关部署——App 所需能力全部由 hs 承载——签发时省略
`gw`。此时：

- **App 只使用 `hs`**；兑换端点只挂在 hs 域名下（`POST {hs}/_botville/pair/redeem`），
  **`gw` 从不承载兑换**——无论二维码里有没有 `gw`，App 都不得向任何 gw 地址发
  兑换请求（§2 的"兑换端点挂在二维码中 hs 域名下"对两种部署同样成立）；
- 签发时无 `gw` 的配对，成功响应的 `gateway` 字段为 **`null`**：字段本身仍必须
  出现（OpenAPI 中 `required` 且 `type: [string, "null"]`），只是值为 null；
- `features_url` 仍为必填的 `https://` URL（指向该部署实际承载功能清单的服务）。

完整示例：无 `gw` 的有效二维码 `examples/pairing-qr/uri-valid-no-gw.txt`，对应
`gateway: null` 的成功响应 `examples/pairing-qr/redeem-200-no-gateway.json`。

### 5.2 hs 一致性规则（防篡改与配置漂移）

- **签发时**：把管理面确认的 `hs` **逐字符**存入配对记录；
- **兑换时**：响应中的 `homeserver` **原样返回**存储值，**不得按当前配置重算**——
  即使部署期间配置变了（域名迁移、换端口），也返回旧值让 App 明确拒绝，而不是
  静默把 App 引向新地址；
- **App 校验**：`resp.homeserver` 必须与二维码 URI 中的 `hs` **逐字符完全一致**
  （不做规范化：`https://hs.example.org/` ≠ `https://hs.example.org`，多一个斜杠
  即拒绝）。不一致时 App 丢弃响应、不使用 token，并提示配对码与服务器不一致。

### 5.3 App 侧错误处理

| 结果 | App 行为 |
|---|---|
| 200 + hs 一致 | 进入注册/登录流程 |
| 200 + hs 不一致 | 拒绝（§5.2），提示管理员重新签发 |
| 证书指纹 / 主机名（SAN）/ 有效期 / EKU 校验失败 | **App 端**证书错误（如 `certificateMismatch`；**不是** §4.4 的 `BV_PAIR_*`）：提示证书不匹配，**不重试、不绕过、不当作一般网络失败** |
| 400 `BV_PAIR_INVALID` | 提示码无效，停止（不重试） |
| 409 `BV_PAIR_USED` | 提示码已被使用（可能是别人扫了），停止 |
| 410 `BV_PAIR_EXPIRED` / `BV_PAIR_REVOKED` | 提示过期/已作废，请管理员重新签发 |
| 429 `BV_PAIR_RATE_LIMITED` | 按 `retry_after_ms` 等待后可重试（缺 `retry_after_ms` 视为服务端 bug，按本地退避） |

## 6. 安全说明（汇总）

- 二维码不含长期凭据（§3.6）；`code` 一次性、5 分钟、只存哈希。
- 兑换仅在 `https://` 之上（`hs` 强制 https）；`fp` 供自签/私有 CA 部署锁定证书。
  有 `fp` 时：SPKI pin **替代**系统根，并须叶证自锚 + SAN/有效期/serverAuth EKU
  （§3.3.2）；作用域为 `(scheme, host, port)`，`gw` 仅同源才复用 pin。无 `fp` 时仅
  系统信任。配对 pin ≠ 全链路 pin；SDK 同步仍无法 pin（§3.3.3）。
- 查无 code 与格式错同码 400，不泄露 code 存在性；限流先于查询（§4.5）。
- 原子核销杜绝双花（§4.3）；审计不含 code 明文与 token（§4.6）。
- 管理面不暴露公网匿名访问：Matrix 管理员登录或 loopback CLI + 一次性 token（§4.1）。
- 示例与测试中的 token 均为 `<redacted-example>` 占位；测试对示例目录做"疑似密钥
  简单扫描"（PEM 块、长十六进制串、非占位 token 字段）防真实凭据误入。

## 7. 文件索引

| 文件 | 角色 |
|---|---|
| `docs/contracts/pairing-qr.md` | 本文档：人类可读契约（URI、生命周期、兑换、安全） |
| `docs/contracts/pairing-redeem.openapi.yaml` | OpenAPI 3.1 片段：redeem 请求/响应/全部错误（机器可读，冲突以此为准） |
| `docs/contracts/examples/pairing-qr/` | mock 示例：3 个有效 URI、8 个无效 URI、兑换请求、3 个 200、5 个错误体 |
| `server/pairing_qr_contract.py` | 标准库参考实现：`parse_pair_uri` / `validate_redeem_response` / `validate_error` |
| `server/tests/test_pairing_qr_contract.py` | 单测：示例分类、拒绝规则、hs 一致性、429 必带字段、OpenAPI 错误码覆盖、密钥扫描 |

## 8. 不在本契约内（后续 PR）

- 服务端实现（存储、HTTP 端点、限流、管理页/CLI）——按本契约与 OpenAPI 落地；
- App 扫码 UI 与流程引导（C2.2）；
- App TLS 实现对齐 §3.3.2（自锚 + SAN/有效期/EKU + `(scheme,host,port)` 作用域；#132-PR2）；
- SDK 同步在自签环境下的连通性（注入根 spike / 描述文件 / 上游需求，见 §3.3.3）；
- `registration_token` / `login_token` 在 homeserver 侧的核销实现；
- `features_url` 指向的功能清单内容结构（网关契约）；
- 管理页对签发记录的列表/筛选/批量作废等运维功能细节。
