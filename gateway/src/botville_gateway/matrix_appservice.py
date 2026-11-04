# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 botville contributors
"""以 Matrix application service 身份投递消息的传输实现（A1.3）。

只使用 Matrix 标准接口（Application Service API + CS API masquerade），不碰
`/_synapse/admin`，也不依赖 admin room 命令：

  - `POST /_matrix/client/v3/register` + `m.login.application_service`：
    在注册文件声明的 exclusive user namespace 内免密建号；
  - `?user_id=` masquerade：以 `@ai_*` 身份 join / send；
  - `GET /_matrix/client/v3/directory/room/{alias}`：把 conversation 别名解析
    成 room id（进程内缓存，TTL 默认 60s，`room_cache_ttl_seconds` 可覆盖）；
  - `GET /_matrix/client/v3/account/whoami`：带 AS token 的健康检查。

注册文件由部署层渲染（`server/compose/appservice/botville-gateway.yaml.tmpl`
→ `init.sh` → `generated/appservice/`，见 docs/deploy/appservice.md）。

身份与房间映射（PR #99 审查阻塞项 1，语义见 docs/deploy/appservice.md）：
  - `sender_id` 以 `@` 开头 → 必须落在本 server 的 `@ai_*` 命名空间；
  - 否则视为 persona id，字符集 `^[a-z0-9._=-]{1,64}$`（大写即非法，不做
    静默小写化——先按字符集拒绝，再谈归一化），映射为
    `@ai_<persona_id>:<server_name>`；
  - `room_id` 以 `!` 开头 → 直接使用；否则视为 conversation id（字符集同上），
    解析房间别名 `#<room_alias_prefix><conversation_id>:<server_name>`；别名
    不存在 → `DeliveryError(code="conversation.not_found")`。

安全：`as_token` 等同整个 `@ai_*` 命名空间的管理员凭据，本模块不把它写进
任何日志或异常消息（`DeliveryError.detail` 只含 HS 返回的错误摘要与路径）。
命名空间外的 sender 在客户端侧直接拒绝，不发起请求；HTTP 重定向一律不跟随
（防止 Authorization 头被转发到跨源地址），也不吃环境变量里的代理配置。
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Set, Tuple

from .matrix import DeliveryError, SentEvent, resolve_txn_id

# 注册文件里 exclusive user namespace 的 localpart 前缀（与
# server/compose/appservice/botville-gateway.yaml.tmpl 保持一致）。
AI_NAMESPACE_PREFIX = "@ai_"

# conversation 别名 localpart 的默认前缀（GATEWAY_ROOM_ALIAS_PREFIX 可覆盖）。
DEFAULT_ROOM_ALIAS_PREFIX = "botville_conv_"

# 别名 → room_id 缓存的默认 TTL 秒数（构造参数 room_cache_ttl_seconds /
# GATEWAY_ROOM_ALIAS_CACHE_TTL 可覆盖；0 = 不缓存，负数拒绝）。与
# config.DEFAULT_ROOM_ALIAS_CACHE_TTL 保持一致。
DEFAULT_ROOM_CACHE_TTL_SECONDS = 60.0

# 缓存过期用单调时钟（不受系统时间回拨影响）；模块级绑定便于单测推进时间。
_monotonic = time.monotonic

# persona / conversation id 的字符集（小写即合法；大写在映射前就被拒绝，
# 不做静默小写化，避免调用方把 "Persona" 与 "persona" 当两个身份）。
LOCAL_ID_RE = re.compile(r"^[a-z0-9._=-]{1,64}$")


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """禁止跟随重定向：AS token 在 Authorization 头里，跟随 3xx 可能把凭据
    转发到跨源地址。重定向在这里永远是配置错误（比如把 URL 指到了反代跳转），
    报错比悄悄跟随更安全。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        raise urllib.error.HTTPError(
            req.full_url, code, "redirects are not followed", headers, fp
        )


class AppServiceMatrixTransport:
    """满足 `MatrixTransport` Protocol 的 appservice 传输（标准库实现）。

    线程安全：HTTP 雏形是多线程服务器，`ensure_user` 缓存与别名缓存的
    读写持锁。
    """

    def __init__(
        self,
        *,
        homeserver_url: str,
        as_token: str,
        server_name: str,
        namespace_prefix: str = AI_NAMESPACE_PREFIX,
        room_alias_prefix: str = DEFAULT_ROOM_ALIAS_PREFIX,
        room_cache_ttl_seconds: float = DEFAULT_ROOM_CACHE_TTL_SECONDS,
        timeout: float = 30.0,
        health_timeout: float = 2.0,
        health_cache_seconds: float = 5.0,
    ) -> None:
        if room_cache_ttl_seconds < 0:
            raise ValueError(
                "room_cache_ttl_seconds must be >= 0 (0 disables the "
                "alias->room cache), got {!r}".format(room_cache_ttl_seconds)
            )
        self._base = homeserver_url.rstrip("/")
        self._as_token = as_token
        self._server_name = server_name
        self._prefix = namespace_prefix
        self._room_alias_prefix = room_alias_prefix
        self._room_cache_ttl = room_cache_ttl_seconds
        self._timeout = timeout
        # /health 专用：whoami 用短超时 + 结果缓存（#99 加固 2）。/health 可能被
        # 探针高频轮询，HS 慢或挂起时不能把每个请求都拖住整个 timeout。
        self._health_timeout = health_timeout
        self._health_cache_seconds = health_cache_seconds
        self._health_cached: Optional[bool] = None
        self._health_checked_at = 0.0
        self._health_inflight: Optional[threading.Event] = None
        self._health_result: bool = False
        self._ensured: Set[str] = set()
        # conversation_id -> (room_id, 过期时刻 monotonic 秒)；TTL=0 时不写入。
        self._rooms: Dict[str, Tuple[str, float]] = {}
        self._lock = threading.Lock()
        # 不跟随重定向；忽略 http_proxy 等环境变量（部署网关的机器上代理
        # 配置与 homeserver 内网地址无关，走代理反而会泄漏 AS token）。
        self._opener = urllib.request.build_opener(
            _NoRedirectHandler, urllib.request.ProxyHandler({})
        )

    # ----------------------------------------------------------- low level
    def _request(
        self,
        method: str,
        path: str,
        *,
        body: Optional[Dict[str, Any]] = None,
        query: Optional[Dict[str, str]] = None,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        """一次 HTTP 调用；2xx 返回解析后的 JSON（空响应→{}），否则抛 DeliveryError。"""
        url = self._base + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        headers["Authorization"] = "Bearer " + self._as_token
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with self._opener.open(req, timeout=self._timeout if timeout is None else timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                parsed = json.loads(raw) if raw else {}
            except ValueError:
                parsed = {}
            if not isinstance(parsed, dict):
                parsed = {}
            errcode = parsed.get("errcode") or f"http.{exc.code}"
            # detail 只含 HS 的错误摘要与路径；不含 URL query（无敏感值）与 token。
            detail = parsed.get("error") or f"HTTP {exc.code} from {method} {path}"
            raise DeliveryError(errcode, str(detail)) from exc
        except urllib.error.URLError as exc:
            raise DeliveryError(
                "matrix.unreachable", f"{method} {path}: {exc.reason}"
            ) from exc
        except OSError as exc:
            # 读超时（socket.timeout）等未被 URLError 包裹的网络错误。
            raise DeliveryError(
                "matrix.unreachable", f"{method} {path}: {exc}"
            ) from exc
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except ValueError as exc:
            # 2xx 但不是 JSON：按投递失败处理，不把半懂不懂的响应当成功。
            raise DeliveryError(
                "appservice.response", f"{method} {path}: non-JSON 2xx response"
            ) from exc
        if not isinstance(parsed, dict):
            raise DeliveryError(
                "appservice.response", f"{method} {path}: 2xx response is not a JSON object"
            )
        return parsed

    # ---------------------------------------------------------- namespace
    def _check_sender(self, sender_id: str) -> None:
        """客户端侧校验：sender 必须是本服务器命名空间内的 @ai_* 账号。

        HS 也会拒绝越权 masquerade（M_EXCLUSIVE），但先在本地拦下能避免
        无意义请求，也让配置错误尽早暴露。
        """
        if not sender_id.startswith(self._prefix):
            raise DeliveryError(
                "appservice.namespace",
                f"sender {sender_id!r} does not use the appservice prefix {self._prefix!r}",
            )
        localpart, sep, domain = sender_id[len(self._prefix):].partition(":")
        if not sep or not localpart or domain != self._server_name:
            raise DeliveryError(
                "appservice.namespace",
                f"sender {sender_id!r} is not an @ai_* user of this homeserver "
                f"({self._server_name!r})",
            )

    # ------------------------------------------------------ id mapping
    def resolve_sender(self, sender_id: str) -> str:
        """网关 API 的 sender 标识 → Matrix 用户 ID。

        `@` 开头：按 Matrix 用户 ID 校验（必须已在 @ai_* 命名空间内）；
        否则视为 persona id：字符集 `^[a-z0-9._=-]{1,64}$`（大写即非法，
        先拒绝非法字符、不做任何小写化），映射 `@ai_<persona>:<server>`。
        """
        if sender_id.startswith("@"):
            self._check_sender(sender_id)
            return sender_id
        if not LOCAL_ID_RE.fullmatch(sender_id):
            raise DeliveryError(
                "appservice.namespace",
                f"sender_persona_id {sender_id!r} is neither an @ai_* Matrix user id "
                f"nor a persona id matching ^[a-z0-9._=-]{{1,64}}$",
            )
        return f"{self._prefix}{sender_id}:{self._server_name}"

    def resolve_room(self, room_id: str) -> str:
        """网关 API 的 conversation 标识 → Matrix room ID。

        `!` 开头：直接当 room id 用；否则视为 conversation id（字符集同
        persona），解析房间别名 `#<room_alias_prefix><id>:<server_name>`
        （`GET /_matrix/client/v3/directory/room/{alias}`，带 AS token）。
        成功后进程内缓存（TTL `room_cache_ttl_seconds`，默认 60s；过期后
        重新 lookup 并刷新，别名重指向不至于被永久缓存；0 = 不缓存）。
        别名不存在 → `conversation.not_found`。
        """
        if room_id.startswith("!"):
            return room_id
        if not LOCAL_ID_RE.fullmatch(room_id):
            raise DeliveryError(
                "conversation.invalid",
                f"conversation_id {room_id!r} is neither a Matrix room id (!...) "
                f"nor a conversation id matching ^[a-z0-9._=-]{{1,64}}$",
            )
        # TTL=0 时 _rooms 永远是空的：每次都走 directory lookup。
        with self._lock:
            cached = self._rooms.get(room_id)
        if cached is not None and cached[1] > _monotonic():
            return cached[0]
        alias = f"#{self._room_alias_prefix}{room_id}:{self._server_name}"
        try:
            resp = self._request(
                "GET",
                "/_matrix/client/v3/directory/room/{}".format(
                    urllib.parse.quote(alias, safe="")
                ),
            )
        except DeliveryError as exc:
            if exc.code == "M_NOT_FOUND":
                raise DeliveryError(
                    "conversation.not_found",
                    f"room alias {alias} does not exist on this homeserver",
                ) from exc
            raise
        resolved = resp.get("room_id")
        if not isinstance(resolved, str) or not resolved.startswith("!"):
            raise DeliveryError(
                "appservice.response",
                f"directory response for {alias} has no room_id",
            )
        if self._room_cache_ttl > 0:
            with self._lock:
                self._rooms[room_id] = (resolved, _monotonic() + self._room_cache_ttl)
        return resolved

    @staticmethod
    def _localpart(user_id: str) -> str:
        return user_id[1:].split(":", 1)[0]

    # ------------------------------------------------------------- actions
    def ensure_user(self, user_id: str) -> None:
        """命名空间内免密注册（幂等；M_USER_IN_USE 视为已存在）。进程内缓存。

        200 响应必须带与请求一致的 `user_id`，否则按命名空间错误处理
        （不缓存失败结果）。

        并发（#99 加固 3）：缓存读写在 `self._lock` 下；注册请求本身在锁外，
        因为它是网络调用。双检语义：锁内读缓存 → 锁外注册 → 锁内再写入，
        中途别的线程可能已写入同一 user_id，此时重复写是幂等的。代价是同一
        user_id 的并发首次注册可能各发一次请求，HS 侧 M_USER_IN_USE 兜底。
        """
        self._check_sender(user_id)
        with self._lock:
            if user_id in self._ensured:
                return
        try:
            resp = self._request(
                "POST",
                "/_matrix/client/v3/register",
                body={
                    "type": "m.login.application_service",
                    "username": self._localpart(user_id),
                    "inhibit_login": True,
                },
            )
        except DeliveryError as exc:
            if exc.code != "M_USER_IN_USE":
                raise
            resp = None  # 已存在：HS 侧就是请求的 localpart，无响应可核对
        if resp is not None and resp.get("user_id") != user_id:
            # 防御性检查：HS 建出的账号必须就是请求的那个。
            raise DeliveryError(
                "appservice.namespace",
                f"homeserver registered {resp.get('user_id')!r} for requested {user_id!r}",
            )
        with self._lock:
            self._ensured.add(user_id)

    def join(self, room_id: str, user_id: str) -> str:
        """以 `user_id` 身份加入房间（AS masquerade）。返回 room_id。"""
        self._check_sender(user_id)
        resp = self._request(
            "POST",
            "/_matrix/client/v3/join/{}".format(urllib.parse.quote(room_id, safe="")),
            body={},
            query={"user_id": user_id},
        )
        return resp.get("room_id") or room_id

    def send_text(
        self,
        *,
        room_id: str,
        sender_id: str,
        text: str,
        reply_to: Optional[str] = None,
        content_type: str = "text",
        txn_id: Optional[str] = None,
    ) -> SentEvent:
        sender = self.resolve_sender(sender_id)
        room = self.resolve_room(room_id)
        self.ensure_user(sender)

        content: Dict[str, Any] = {"msgtype": "m.text", "body": text}
        if content_type == "markdown":
            # 不把 markdown 降级为 text（#92）：body 保持原文，另带自定义字段
            # 标注类型；markdown→HTML 转换不在网关范围。
            content["org.botville.content_type"] = "markdown"
        if reply_to:
            content["m.relates_to"] = {"m.in_reply_to": {"event_id": reply_to}}

        # txnId 由调用方的 Idempotency-Key 稳定推导（#99 加固 1）：同一 key
        # 的重试打到同一事务，HS 按 (room, sender, txnId) 去重，不会重复消息。
        txn = resolve_txn_id(txn_id)
        path = "/_matrix/client/v3/rooms/{}/send/m.room.message/{}".format(
            urllib.parse.quote(room, safe=""), urllib.parse.quote(txn, safe="")
        )
        try:
            resp = self._request("PUT", path, body=content, query={"user_id": sender})
        except DeliveryError as exc:
            # 未加入房间（M_FORBIDDEN）时自动 join 后重试一次；其余错误直接上抛。
            if exc.code != "M_FORBIDDEN":
                raise
            self.join(room, sender)
            resp = self._request("PUT", path, body=content, query={"user_id": sender})

        event_id = resp.get("event_id")
        if not event_id:
            raise DeliveryError(
                "appservice.response", "send succeeded but no event_id in response"
            )
        return SentEvent(
            event_id=event_id,
            room_id=room,
            sender_id=sender,
            text=text,
            sent_at=datetime.now(timezone.utc),
            reply_to=reply_to,
            content_type=content_type,
            txn_id=txn,
        )

    def healthy(self) -> bool:
        """带 AS 身份的健康检查：whoami 校验可达性 **和** token 有效性。

        （只打 /versions 的话，token 配错也返回 ok，/health 会误报。）

        #99 加固 2：whoami 用 `health_timeout`（默认 2s）而不是
        `timeout`（默认 30s）——`/health` 被探针高频轮询，HS 挂起时不能让
        探针请求排队等满 30 秒。结果缓存 `health_cache_seconds`（默认 5s）：
        缓存期内直接返回上次结论，不重复打 HS。
        """
        # #114 PR-a：single-flight。缓存过期瞬间只有一个线程（领头者）打 whoami，
        # 其余线程等同一结果，防止免鉴权的 /health 被并发探针放大成 N 次 HS 请求。
        now = time.monotonic()
        with self._lock:
            if (
                self._health_cached is not None
                and now - self._health_checked_at < self._health_cache_seconds
            ):
                return self._health_cached
            inflight = self._health_inflight
            leader = inflight is None
            if leader:
                inflight = threading.Event()
                self._health_inflight = inflight
        if not leader:
            if not inflight.wait(self._health_timeout + 1):
                return False
            with self._lock:
                return bool(self._health_result)
        ok = False
        try:
            self._request(
                "GET",
                "/_matrix/client/v3/account/whoami",
                timeout=self._health_timeout,
            )
            ok = True
        except DeliveryError:
            ok = False
        except OSError:
            # _request 已尽量归一为 DeliveryError；这里兜底，/health 不因
            # 传输层异常变成 500。
            ok = False
        finally:
            with self._lock:
                self._health_cached = ok
                self._health_result = ok
                self._health_checked_at = time.monotonic()
                self._health_inflight = None
            inflight.set()
        return ok
