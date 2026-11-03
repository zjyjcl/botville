# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 botville contributors
"""以 Matrix application service 身份投递消息的传输实现（A1.3）。

只使用 Matrix 标准接口（Application Service API + CS API masquerade），不碰
`/_synapse/admin`，也不依赖 admin room 命令：

  - `POST /_matrix/client/v3/register` + `m.login.application_service`：
    在注册文件声明的 exclusive user namespace 内免密建号；
  - `?user_id=` masquerade：以 `@ai_*` 身份 join / send；
  - `GET /_matrix/client/versions`：健康检查。

注册文件由部署层渲染（`server/compose/appservice/botville-gateway.yaml.tmpl`
→ `init.sh` → `generated/appservice/`，见 docs/deploy/appservice.md）。

安全：`as_token` 等同整个 `@ai_*` 命名空间的管理员凭据，本模块不把它写进
任何日志或异常消息（`DeliveryError.detail` 只含 HS 返回的错误摘要与路径）。
命名空间外的 sender 在客户端侧直接拒绝，不发起请求。
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Set

from .matrix import DeliveryError, SentEvent

# 注册文件里 exclusive user namespace 的 localpart 前缀（与
# server/compose/appservice/botville-gateway.yaml.tmpl 保持一致）。
AI_NAMESPACE_PREFIX = "@ai_"


class AppServiceMatrixTransport:
    """满足 `MatrixTransport` Protocol 的 appservice 传输（标准库实现）。

    线程安全：HTTP 雏形是多线程服务器，`ensure_user` 缓存的读写持锁。
    """

    def __init__(
        self,
        *,
        homeserver_url: str,
        as_token: str,
        server_name: str,
        namespace_prefix: str = AI_NAMESPACE_PREFIX,
        timeout: float = 30.0,
    ) -> None:
        self._base = homeserver_url.rstrip("/")
        self._as_token = as_token
        self._server_name = server_name
        self._prefix = namespace_prefix
        self._timeout = timeout
        self._ensured: Set[str] = set()
        self._lock = threading.Lock()

    # ----------------------------------------------------------- low level
    def _request(
        self,
        method: str,
        path: str,
        *,
        body: Optional[Dict[str, Any]] = None,
        query: Optional[Dict[str, str]] = None,
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
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else {}
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

    @staticmethod
    def _localpart(user_id: str) -> str:
        return user_id[1:].split(":", 1)[0]

    # ------------------------------------------------------------- actions
    def ensure_user(self, user_id: str) -> None:
        """命名空间内免密注册（幂等；M_USER_IN_USE 视为已存在）。进程内缓存。"""
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
            resp = {}
        got = resp.get("user_id")
        if got and got != user_id:
            # 防御性检查：HS 建出的账号必须就是请求的那个。
            raise DeliveryError(
                "appservice.namespace",
                f"homeserver registered {got!r} for requested {user_id!r}",
            )
        with self._lock:
            self._ensured.add(user_id)

    def join(self, room_id: str, user_id: str) -> str:
        """以 `user_id` 身份加入房间（AS masquerade）。返回 room_id。"""
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
    ) -> SentEvent:
        self._check_sender(sender_id)
        self.ensure_user(sender_id)

        content: Dict[str, Any] = {"msgtype": "m.text", "body": text}
        if content_type == "markdown":
            # 不把 markdown 降级为 text（#92）：body 保持原文，另带自定义字段
            # 标注类型；markdown→HTML 转换不在网关范围。
            content["org.botville.content_type"] = "markdown"
        if reply_to:
            content["m.relates_to"] = {"m.in_reply_to": {"event_id": reply_to}}

        path = "/_matrix/client/v3/rooms/{}/send/m.room.message/{}".format(
            urllib.parse.quote(room_id, safe=""), uuid.uuid4().hex
        )
        try:
            resp = self._request("PUT", path, body=content, query={"user_id": sender_id})
        except DeliveryError as exc:
            # 未加入房间（M_FORBIDDEN）时自动 join 后重试一次；其余错误直接上抛。
            if exc.code != "M_FORBIDDEN":
                raise
            self.join(room_id, sender_id)
            resp = self._request("PUT", path, body=content, query={"user_id": sender_id})

        event_id = resp.get("event_id")
        if not event_id:
            raise DeliveryError(
                "appservice.response", "send succeeded but no event_id in response"
            )
        return SentEvent(
            event_id=event_id,
            room_id=room_id,
            sender_id=sender_id,
            text=text,
            sent_at=datetime.now(timezone.utc),
            reply_to=reply_to,
            content_type=content_type,
        )

    def healthy(self) -> bool:
        try:
            self._request("GET", "/_matrix/client/versions")
        except DeliveryError:
            return False
        except OSError:
            # _request 已尽量归一为 DeliveryError；这里兜底，/health 不因
            # 传输层异常变成 500。
            return False
        return True
