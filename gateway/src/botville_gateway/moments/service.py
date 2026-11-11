# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 botville contributors
"""动态服务层（A2.2 / issue #7 PR2：同服实现）。

公开操作（HTTP 分发调用，viewer 恒为 Matrix user id）与 AI 内部调用
（appservice 代发，不走 Matrix 用户令牌）都在这里；可见性判定全部委托
`visibility` 模块（单一函数），越权与不存在统一抛 `MomentNotFound`，
由 HTTP 层构造**逐字节相同**的 404 响应体。

幂等：POST /moments 与 POST …/comments 按 (scope, viewer, key) 进程内缓存，
同体（规范化 JSON 指纹一致）重放返回同一结果，异体抛 `IdempotencyConflict`
（409 idempotency.conflict）；TTL / 容量沿用网关既有配置。

审计：create / update_visibility / delete / comment / delete_comment /
like / unlike 全量落 `moments_events`；返回 404 但动态实际存在的越权尝试
按 (viewer, moment) 60 秒节流后记 action=denied。审计不写任何 token。
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import threading
import time as _time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from ..config import Config
from ..errors import IdempotencyConflict, UnprocessableError, ValidationError
from ..timeutil import iso_utc, utcnow
from .migrations import apply_migrations, connect
from .store import CommentRow, MomentRow, MomentStore
from .ulid import is_ulid, new_ulid
from .visibility import (
    RELATION_TO_VISIBILITY,
    EmptyPersonaDirectory,
    PersonaDirectory,
    RelationProvider,
    StaticRelationProvider,
    can_view,
    comment_visible,
    like_visible,
)

VISIBILITIES = ("self", "friends", "server", "paired_servers")

MAX_BODY_CHARS = 2000
MAX_COMMENT_CHARS = 500
MAX_MEDIA_ITEMS = 9
MAX_ALLOW_LIST = 200

# 越权尝试审计的节流窗口（同一 (viewer, moment) 只记一次）。
DENIED_THROTTLE_SECONDS = 60.0


class MomentNotFound(Exception):
    """动态不存在或对调用者不可见（HTTP 层统一 404 moment.not_found）。"""


class PersonaNotOwned(Exception):
    """author_persona_id 不归调用者所有（403 persona.not_owned）。"""


class RevisionConflict(Exception):
    """PATCH 的 revision 与服务端不一致（409 moment.revision_conflict）。"""


_log = logging.getLogger(__name__)

# ---------------------------------------------------------------- 游标

def encode_cursor(payload: Dict[str, Any]) -> str:
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


# 各列表游标的必需字段：m=动态（created_at, id），c=评论（created_at, id），
# l=点赞（created_at, actor_id）。字段值必须是非空字符串。
_CURSOR_FIELDS = {"m": ("c", "i"), "c": ("c", "i"), "l": ("c", "a")}
_CURSOR_FIELD_MAX = 512


def decode_cursor(cursor: str, kind: str) -> Dict[str, Any]:
    """不透明游标解析；非法（非 base64 / 非 JSON / kind 不符 / 缺字段）→
    400 request.invalid（契约：cursor 不透明，非法值是请求错误）。"""
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except Exception as exc:
        raise ValidationError("request.invalid", "invalid cursor") from exc
    if not isinstance(payload, dict) or payload.get("k") != kind:
        raise ValidationError("request.invalid", "invalid cursor")
    # 合法 base64 + JSON 但缺字段 / 类型不对，同样是请求错误（400），
    # 不能在后续 params["c"] 取值或元组比较时抛 KeyError / TypeError → 500。
    for field in _CURSOR_FIELDS[kind]:
        value = payload.get(field)
        if not isinstance(value, str) or not value or len(value) > _CURSOR_FIELD_MAX:
            raise ValidationError("request.invalid", "invalid cursor")
    return payload


# ---------------------------------------------------------------- 校验

def _require_str(
    payload: Dict[str, Any], field: str, *, required: bool = True
) -> Optional[str]:
    value = payload.get(field)
    if value is None:
        if required:
            raise ValidationError("request.invalid", f"'{field}' is required", field)
        return None
    if not isinstance(value, str) or not value:
        raise ValidationError(
            "request.invalid", f"'{field}' must be a non-empty string", field
        )
    return value


def _reject_unknown(payload: Dict[str, Any], allowed: Sequence[str]) -> None:
    for key in payload:
        if key not in allowed:
            raise ValidationError(
                "request.invalid", f"unknown field '{key}'", key
            )


def _validate_media(value: Any) -> Tuple[Dict[str, Any], ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValidationError(
            "request.invalid", "'media' must be an array", "media"
        )
    if len(value) > MAX_MEDIA_ITEMS:
        raise ValidationError(
            "request.invalid",
            f"'media' must have at most {MAX_MEDIA_ITEMS} items", "media",
        )
    items: List[Dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            raise ValidationError(
                "request.invalid", "'media' items must be objects", "media"
            )
        mxc = item.get("mxc_uri")
        if not isinstance(mxc, str) or not mxc.startswith("mxc://"):
            raise ValidationError(
                "request.invalid",
                "'media[].mxc_uri' must be an mxc:// URI", "media",
            )
        mimetype = item.get("mimetype")
        if not isinstance(mimetype, str) or not mimetype:
            raise ValidationError(
                "request.invalid", "'media[].mimetype' is required", "media"
            )
        clean: Dict[str, Any] = {"mxc_uri": mxc, "mimetype": mimetype}
        for dim in ("width", "height"):
            dim_value = item.get(dim)
            if dim_value is None:
                continue
            if (
                not isinstance(dim_value, int)
                or isinstance(dim_value, bool)
                or dim_value < 1
            ):
                raise ValidationError(
                    "request.invalid",
                    f"'media[].{dim}' must be a positive integer", "media",
                )
            clean[dim] = dim_value
        items.append(clean)
    return tuple(items)


def _validate_allow_list(value: Any) -> Optional[Tuple[str, ...]]:
    if value is None:
        return None
    if not isinstance(value, list):
        raise ValidationError(
            "request.invalid", "'allow_list' must be an array or null", "allow_list"
        )
    if len(value) > MAX_ALLOW_LIST:
        raise ValidationError(
            "request.invalid",
            f"'allow_list' must have at most {MAX_ALLOW_LIST} items", "allow_list",
        )
    for item in value:
        if not isinstance(item, str) or not item:
            raise ValidationError(
                "request.invalid",
                "'allow_list' items must be non-empty strings", "allow_list",
            )
    return tuple(value)


def _validate_visibility(value: Any) -> str:
    if not isinstance(value, str) or value not in VISIBILITIES:
        raise ValidationError(
            "request.invalid",
            f"'visibility' must be one of {list(VISIBILITIES)}", "visibility",
        )
    return value


@dataclass
class _IdemEntry:
    fingerprint: str
    response: Dict[str, Any]
    expires_at: float  # monotonic 时间戳


class MomentsService:
    """动态业务服务。所有公开方法的 viewer 均为 Matrix user id。"""

    def __init__(
        self,
        *,
        store: MomentStore,
        relations: RelationProvider,
        persona_dir: Optional[PersonaDirectory] = None,
        server_name: Optional[str] = None,
        idempotency_ttl_seconds: int = 86_400,
        max_idempotency_entries: int = 2_000,
        soft_delete_retention_days: int = 30,
        audit_retention_days: int = 90,
        now_fn: Callable[[], datetime] = utcnow,
        ulid_fn: Callable[[], str] = new_ulid,
        monotonic_fn: Callable[[], float] = _time.monotonic,
    ) -> None:
        self.store = store
        self.relations = relations
        self.persona_dir: PersonaDirectory = persona_dir or EmptyPersonaDirectory()
        self._server_name = server_name
        self._idem_ttl = max(0, idempotency_ttl_seconds)
        self._idem_max = max(1, max_idempotency_entries)
        self._soft_days = soft_delete_retention_days
        self._audit_days = audit_retention_days
        self._now = now_fn
        self._ulid = ulid_fn
        self._monotonic = monotonic_fn
        self._idem: Dict[Tuple[str, str, str], _IdemEntry] = {}
        self._idem_lock = threading.Lock()
        # (viewer, moment_id) → monotonic 时间：denied 审计节流。
        self._denied_last: Dict[Tuple[str, str], float] = {}
        self._denied_lock = threading.Lock()

    # ------------------------------------------------------------ helpers
    def _audit(
        self,
        actor: str,
        action: str,
        moment_id: Optional[str] = None,
        detail: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.store.append_event(
            at=iso_utc(self._now()),
            moment_id=moment_id,
            actor=actor,
            action=action,
            detail_json=json.dumps(detail) if detail is not None else None,
        )

    def _audit_denied(self, viewer: str, moment_id: str) -> None:
        """越权尝试审计：同一 (viewer, moment) 60 秒内只记一次。"""
        now = self._monotonic()
        key = (viewer, moment_id)
        with self._denied_lock:
            last = self._denied_last.get(key)
            if last is not None and now - last < DENIED_THROTTLE_SECONDS:
                return
            self._denied_last[key] = now
        self._audit(viewer, "denied", moment_id)

    def _load_visible_moment(self, viewer: str, moment_id: str) -> MomentRow:
        """加载动态并判 can_view；不存在 / 软删 / 不可见 → MomentNotFound。

        动态实际存在且未删但被拒时记 denied 审计（节流）。
        """
        if not is_ulid(moment_id):
            raise MomentNotFound(moment_id)
        moment = self.store.get_post(moment_id)
        if moment is None:
            raise MomentNotFound(moment_id)
        if not can_view(viewer, moment, self.relations, server_name=self._server_name):
            if moment.deleted_at is None:
                self._audit_denied(viewer, moment_id)
            raise MomentNotFound(moment_id)
        return moment

    def _require_owned_by(self, viewer: str, moment: MomentRow) -> None:
        """改 / 删权限：仅作者本人或所有者；可见但无权同样 404（不泄露区别）。"""
        if viewer != moment.author_id and viewer != moment.owner_id:
            self._audit_denied(viewer, moment.id)
            raise MomentNotFound(moment.id)

    def _author_payload(
        self, author_id: str, kind: str, owner_id: str
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "id": author_id,
            "kind": kind,
            "owner_id": owner_id,  # human 时等于 id（契约可回显）
        }
        if kind == "ai":
            display = self.persona_dir.display_name(author_id)
            if display:
                payload["display_name"] = display
        return payload

    def _moment_response(self, moment: MomentRow, viewer: Optional[str]) -> Dict[str, Any]:
        """Moment 响应实体；计数与 liked_by_me 相对 viewer。"""
        comments = self.store.comments_for_moment(moment.id)
        likes = self.store.likes_for_moment(moment.id)
        visible_comments = [
            c for c in comments
            if comment_visible(
                viewer, moment, c, self.relations, server_name=self._server_name
            )
        ]
        visible_likes = [
            l for l in likes
            if like_visible(
                viewer, moment, l, self.relations, server_name=self._server_name
            )
        ]
        liked_by_me = viewer is not None and any(
            l.actor_id == viewer for l in likes
        )
        response: Dict[str, Any] = {
            "id": moment.id,
            "author": self._author_payload(
                moment.author_id, moment.author_kind, moment.owner_id
            ),
            "owner_id": moment.owner_id,
            "body": moment.body,
        }
        if moment.media:
            response["media"] = [dict(item) for item in moment.media]
        response["visibility"] = moment.visibility
        response["allow_list"] = (
            list(moment.allow_list) if moment.allow_list is not None else None
        )
        response["like_count"] = len(visible_likes)
        response["comment_count"] = len(visible_comments)
        response["liked_by_me"] = liked_by_me
        response["revision"] = moment.revision
        response["created_at"] = moment.created_at
        response["updated_at"] = moment.updated_at
        return response

    def _comment_response(self, comment: CommentRow) -> Dict[str, Any]:
        return {
            "id": comment.id,
            "moment_id": comment.moment_id,
            "author": self._author_payload(
                comment.author_id, comment.author_kind, comment.owner_id
            ),
            "reply_to_comment_id": comment.reply_to_comment_id,
            "body": comment.body,
            "created_at": comment.created_at,
        }

    # ------------------------------------------------------------ 幂等
    @staticmethod
    def _fingerprint(payload: Dict[str, Any]) -> str:
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _idem_get(
        self, scope: str, viewer: str, target: Optional[str], key: str,
        fingerprint: str,
    ) -> Optional[Dict[str, Any]]:
        """幂等缓存查找。作用域 = (scope, viewer, 目标资源, key)：

        与 #114 消息账本 `(conversation_id, sender, txnId)` 同一思路，同一 key
        用在**另一个**目标资源上是另一次请求，不会命中前一个目标的回执。
        调用方必须**先**完成鉴权 / 可见性判定再查缓存，保证重放也遵守 404 规则。
        """
        cache_key = (scope, viewer, target, key)
        with self._idem_lock:
            self._idem_evict_locked()
            entry = self._idem.get(cache_key)
            if entry is None:
                return None
            if self._monotonic() >= entry.expires_at:
                del self._idem[cache_key]
                return None
            if entry.fingerprint != fingerprint:
                raise IdempotencyConflict(key)
            return entry.response

    def _idem_evict_locked(self) -> None:
        now = self._monotonic()
        for stale in [
            k for k, e in self._idem.items() if now >= e.expires_at
        ]:
            del self._idem[stale]
        while len(self._idem) >= self._idem_max:
            self._idem.pop(next(iter(self._idem)))

    def _idem_put(
        self, scope: str, viewer: str, target: Optional[str], key: str,
        fingerprint: str, response: Dict[str, Any],
    ) -> None:
        cache_key = (scope, viewer, target, key)
        with self._idem_lock:
            self._idem_evict_locked()
            self._idem[cache_key] = _IdemEntry(
                fingerprint=fingerprint,
                response=response,
                expires_at=self._monotonic() + self._idem_ttl,
            )

    # ------------------------------------------------------------ 读
    def get_moment(self, viewer: str, moment_id: str) -> Dict[str, Any]:
        moment = self._load_visible_moment(viewer, moment_id)
        return self._moment_response(moment, viewer)

    def list_moments(
        self,
        viewer: str,
        *,
        author: Optional[str] = None,
        cursor: Optional[str] = None,
        limit: int = 20,
    ) -> Dict[str, Any]:
        """可见动态流（时间倒序）。列表 ⊆ {GET 单条会 200 的集合}。"""
        if not 1 <= limit <= 50:
            raise ValidationError(
                "request.invalid", "'limit' must be between 1 and 50", "limit"
            )
        after: Optional[Tuple[str, str]] = None
        if cursor:
            params = decode_cursor(cursor, "m")
            after = (params["c"], params["i"])
        visible: List[MomentRow] = []
        db_key = after
        while len(visible) <= limit:
            rows = self.store.page_posts_desc(
                after_key=db_key, author_id=author, limit=200
            )
            if not rows:
                break
            for row in rows:
                db_key = (row.created_at, row.id)
                if can_view(
                    viewer, row, self.relations, server_name=self._server_name
                ):
                    visible.append(row)
            if len(rows) < 200:
                break
        has_more = len(visible) > limit
        page = visible[:limit]
        next_cursor = None
        if has_more:
            last = page[-1]
            next_cursor = encode_cursor(
                {"k": "m", "c": last.created_at, "i": last.id}
            )
        return {
            "items": [self._moment_response(m, viewer) for m in page],
            "next_cursor": next_cursor,
            "has_more": has_more,
        }

    def list_comments(
        self,
        viewer: str,
        moment_id: str,
        *,
        cursor: Optional[str] = None,
        limit: int = 20,
    ) -> Dict[str, Any]:
        """评论分页（时间正序，只含可见评论）。"""
        if not 1 <= limit <= 50:
            raise ValidationError(
                "request.invalid", "'limit' must be between 1 and 50", "limit"
            )
        moment = self._load_visible_moment(viewer, moment_id)
        comments = self.store.comments_for_moment(moment.id)
        visible = [
            c for c in comments
            if comment_visible(
                viewer, moment, c, self.relations, server_name=self._server_name
            )
        ]
        start = 0
        if cursor:
            params = decode_cursor(cursor, "c")
            cursor_key = (params["c"], params["i"])
            start = len(visible)
            for idx, comment in enumerate(visible):
                if (comment.created_at, comment.id) > cursor_key:
                    start = idx
                    break
        page = visible[start:start + limit]
        has_more = len(visible) > start + limit
        next_cursor = None
        if has_more:
            last = page[-1]
            next_cursor = encode_cursor(
                {"k": "c", "c": last.created_at, "i": last.id}
            )
        return {
            "items": [self._comment_response(c) for c in page],
            "next_cursor": next_cursor,
            "has_more": has_more,
        }

    def list_likes(
        self,
        viewer: str,
        moment_id: str,
        *,
        cursor: Optional[str] = None,
        limit: int = 20,
    ) -> Dict[str, Any]:
        """点赞分页（时间正序，只含可见点赞）。"""
        if not 1 <= limit <= 50:
            raise ValidationError(
                "request.invalid", "'limit' must be between 1 and 50", "limit"
            )
        moment = self._load_visible_moment(viewer, moment_id)
        likes = self.store.likes_for_moment(moment.id)
        visible = [
            l for l in likes
            if like_visible(
                viewer, moment, l, self.relations, server_name=self._server_name
            )
        ]
        start = 0
        if cursor:
            params = decode_cursor(cursor, "l")
            cursor_key = (params["c"], params["a"])
            start = len(visible)
            for idx, like in enumerate(visible):
                if (like.created_at, like.actor_id) > cursor_key:
                    start = idx
                    break
        page = visible[start:start + limit]
        has_more = len(visible) > start + limit
        next_cursor = None
        if has_more:
            last = page[-1]
            next_cursor = encode_cursor(
                {"k": "l", "c": last.created_at, "a": last.actor_id}
            )
        return {
            "items": [
                {
                    "moment_id": l.moment_id,
                    "actor": self._author_payload(
                        l.actor_id, l.actor_kind, l.owner_id
                    ),
                    "created_at": l.created_at,
                }
                for l in page
            ],
            "next_cursor": next_cursor,
            "has_more": has_more,
        }

    # ------------------------------------------------------------ 写
    def create_moment(
        self,
        viewer: str,
        payload: Dict[str, Any],
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """POST /v1/moments。作者为调用者本人或其名下 AI persona。"""
        # 授权先于幂等缓存：persona 归属被收回后，同 key 重放也必须 403，
        # 不能拿旧回执绕过（与评论「先可见性再查缓存」同一原则）。
        early_persona = payload.get("author_persona_id")
        if isinstance(early_persona, str) and early_persona:
            self._resolve_persona_author(viewer, early_persona)
        if idempotency_key is not None:
            fingerprint = self._fingerprint({"scope": "moment", "body": payload})
            cached = self._idem_get(
                "moment", viewer, None, idempotency_key, fingerprint
            )
            if cached is not None:
                return cached
        _reject_unknown(
            payload,
            ("author_persona_id", "body", "media", "visibility", "allow_list"),
        )
        body = _require_str(payload, "body")
        assert body is not None
        if len(body) > MAX_BODY_CHARS:
            raise ValidationError(
                "request.invalid",
                f"'body' must be at most {MAX_BODY_CHARS} characters", "body",
            )
        media = _validate_media(payload.get("media"))
        allow_list = _validate_allow_list(payload.get("allow_list"))
        visibility = payload.get("visibility")
        if visibility is not None:
            visibility = _validate_visibility(visibility)

        persona_id = payload.get("author_persona_id")
        if persona_id is not None:
            persona_id = _require_str(payload, "author_persona_id")
            assert persona_id is not None
            author_id, author_kind, owner_id, default_visibility = (
                self._resolve_persona_author(viewer, persona_id)
            )
        else:
            author_id = viewer
            author_kind = "human"
            owner_id = viewer
            # 真人作者缺省 friends（moments.md §3）。
            default_visibility = "friends"
        if visibility is None:
            visibility = default_visibility

        created_at = iso_utc(self._now())
        moment = self.store.insert_post(
            id=self._ulid(),
            author_id=author_id,
            author_kind=author_kind,
            owner_id=owner_id,
            body=body,
            media=list(media),
            visibility=visibility,
            allow_list=allow_list,
            created_at=created_at,
        )
        self._audit(
            author_id, "create", moment.id, {"visibility": visibility}
        )
        response = self._moment_response(moment, viewer)
        if idempotency_key is not None:
            self._idem_put(
                "moment", viewer, None, idempotency_key, fingerprint, response
            )
        return response

    def _resolve_persona_author(
        self, viewer: str, persona_id: str
    ) -> Tuple[str, str, str, str]:
        """解析 AI 作者：persona 必须归 viewer 所有，否则 403 persona.not_owned。

        返回 (author_id, 'ai', owner_id, 缺省 visibility)。
        """
        owner = self.persona_dir.owner_of(persona_id)
        if owner is None or owner != viewer:
            raise PersonaNotOwned(persona_id)
        relation_default = self.persona_dir.moments_visibility_default(persona_id)
        default_visibility = RELATION_TO_VISIBILITY.get(
            relation_default, "friends"
        )
        return persona_id, "ai", owner, default_visibility

    def update_moment(
        self, viewer: str, moment_id: str, payload: Dict[str, Any]
    ) -> Dict[str, Any]:
        """PATCH /v1/moments/{id}：只改 visibility / allow_list，revision+1。"""
        _reject_unknown(payload, ("revision", "visibility", "allow_list"))
        revision = payload.get("revision")
        if revision is None:
            raise ValidationError(
                "request.invalid", "'revision' is required", "revision"
            )
        if (
            not isinstance(revision, int)
            or isinstance(revision, bool)
            or revision < 1
        ):
            raise ValidationError(
                "request.invalid", "'revision' must be a positive integer", "revision"
            )
        # 显式 `"visibility": null` 不是合法 Visibility（schema 无 null）→ 400；
        # 省略才表示保持。
        visibility = None
        if "visibility" in payload:
            visibility = _validate_visibility(payload.get("visibility"))
        if "allow_list" in payload:
            allow_list = _validate_allow_list(payload.get("allow_list"))
        else:
            allow_list = _SENTINEL_KEEP  # 省略 = 保持

        moment = self._load_visible_moment(viewer, moment_id)
        self._require_owned_by(viewer, moment)
        if revision != moment.revision:
            raise RevisionConflict(moment.id)
        new_visibility = (
            visibility if visibility is not None else moment.visibility
        )
        if allow_list is _SENTINEL_KEEP:
            new_allow: Optional[Tuple[str, ...]] = moment.allow_list
        else:
            new_allow = allow_list
        updated = self.store.update_post(
            moment.id,
            visibility=new_visibility,
            allow_list=new_allow,
            updated_at=iso_utc(self._now()),
            expected_revision=revision,
        )
        if updated is None:
            # 并发：比对与写入之间有人先改（revision 已变）或已删。
            current = self.store.get_post(moment.id)
            if current is None or current.deleted_at is not None:
                raise MomentNotFound(moment_id)
            raise RevisionConflict(moment.id)
        self._audit(
            viewer, "update_visibility", moment.id,
            {"visibility": new_visibility, "revision": updated.revision},
        )
        return self._moment_response(updated, viewer)

    def delete_moment(self, viewer: str, moment_id: str) -> None:
        """DELETE /v1/moments/{id}：软删，204。"""
        moment = self._load_visible_moment(viewer, moment_id)
        self._require_owned_by(viewer, moment)
        self.store.soft_delete_post(moment.id, iso_utc(self._now()))
        # 审计记实际操作者（owner 删 AI 动态时是 owner，不是 persona）。
        self._audit(viewer, "delete", moment.id)

    # ------------------------------------------------------------ 评论
    def create_comment(
        self,
        viewer: str,
        moment_id: str,
        payload: Dict[str, Any],
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """POST /v1/moments/{id}/comments：行为者恒为 viewer（human）。

        顺序（#184 审查 B1）：先 `_load_visible_moment`（不存在 / 软删 / 不可见
        → 404，含幂等重放），再按 (viewer, moment_id, key) 查幂等缓存；
        moment_id 同时进缓存键与指纹，换目标动态不会重放前一条的 201。
        """
        moment = self._load_visible_moment(viewer, moment_id)
        if idempotency_key is not None:
            fingerprint = self._fingerprint(
                {"scope": "comment", "moment_id": moment.id, "body": payload}
            )
            cached = self._idem_get(
                "comment", viewer, moment.id, idempotency_key, fingerprint
            )
            if cached is not None:
                return cached
        # 公开路径不接受 author_persona_id（契约 additionalProperties:false）。
        _reject_unknown(payload, ("body", "reply_to_comment_id"))
        body = _require_str(payload, "body")
        assert body is not None
        if len(body) > MAX_COMMENT_CHARS:
            raise ValidationError(
                "request.invalid",
                f"'body' must be at most {MAX_COMMENT_CHARS} characters", "body",
            )
        reply_to = self._validate_reply_target(viewer, moment, payload)
        comment = self.store.insert_comment(
            id=self._ulid(),
            moment_id=moment.id,
            author_id=viewer,
            author_kind="human",
            owner_id=viewer,
            body=body,
            reply_to_comment_id=reply_to,
            created_at=iso_utc(self._now()),
        )
        self._audit(viewer, "comment", moment.id, {"comment_id": comment.id})
        response = self._comment_response(comment)
        if idempotency_key is not None:
            self._idem_put(
                "comment", viewer, moment.id, idempotency_key, fingerprint,
                response,
            )
        return response

    def _validate_reply_target(
        self, viewer: str, moment: MomentRow, payload: Dict[str, Any]
    ) -> Optional[str]:
        """reply_to_comment_id 校验：不存在 / 已删 / 不同动态 / 对 viewer 不可见
        统一 422 request.invalid（errors[0].field=reply_to_comment_id），
        不区分原因，避免靠差异探测评论 ULID。"""
        if "reply_to_comment_id" not in payload:
            return None
        value = payload.get("reply_to_comment_id")
        reply_to: Optional[str] = None
        if isinstance(value, str) and value:
            reply_to = value
        elif value is None:
            reply_to = None
        else:
            # 结构类型错（数字 / 对象 / 空串等）按契约 §1.3 是 400；moments.md §6
            # 的统一 422 只覆盖「不存在 / 已软删 / 不同动态 / 不可见」四类语义非法。
            raise ValidationError(
                "request.invalid",
                "'reply_to_comment_id' must be a ULID string",
                "reply_to_comment_id",
            )
        if reply_to is None:
            return None
        target = (
            self.store.get_comment(reply_to)
            if is_ulid(reply_to) else None
        )
        if (
            target is None
            or target.moment_id != moment.id
            or not comment_visible(
                viewer, moment, target, self.relations,
                server_name=self._server_name,
            )
        ):
            raise UnprocessableError(
                "'reply_to_comment_id' must reference a visible comment in the"
                " same moment",
                field="reply_to_comment_id",
            )
        return reply_to

    def delete_comment(
        self, viewer: str, moment_id: str, comment_id: str
    ) -> None:
        """DELETE /v1/moments/{id}/comments/{cid}：评论作者 / 其主人，或动态
        作者 / 所有者可删；其余（含评论不可见）统一 404。"""
        if not is_ulid(comment_id):
            raise MomentNotFound(moment_id)
        moment = self._load_visible_moment(viewer, moment_id)
        comment = self.store.get_comment(comment_id)
        if (
            comment is None
            or comment.deleted_at is not None
            or comment.moment_id != moment.id
            or (
                viewer not in (comment.author_id, comment.owner_id)
                and viewer not in (moment.author_id, moment.owner_id)
            )
        ):
            self._audit_denied(viewer, moment.id)
            raise MomentNotFound(moment_id)
        # 评论对删除者必须可见（can_view ∧ can_see_interaction）。
        if not comment_visible(
            viewer, moment, comment, self.relations,
            server_name=self._server_name,
        ):
            self._audit_denied(viewer, moment.id)
            raise MomentNotFound(moment_id)
        self.store.soft_delete_comment(comment_id, iso_utc(self._now()))
        self._audit(viewer, "delete_comment", moment.id, {"comment_id": comment_id})

    # ------------------------------------------------------------ 点赞
    def like_moment(self, viewer: str, moment_id: str) -> Dict[str, Any]:
        """PUT /v1/moments/{id}/likes/me：幂等。"""
        moment = self._load_visible_moment(viewer, moment_id)
        created = self.store.upsert_like(
            moment_id=moment.id,
            actor_id=viewer,
            actor_kind="human",
            owner_id=viewer,
            created_at=iso_utc(self._now()),
        )
        if created:
            self._audit(viewer, "like", moment.id)
        return {"moment_id": moment.id, "liked": True}

    def unlike_moment(self, viewer: str, moment_id: str) -> Dict[str, Any]:
        """DELETE /v1/moments/{id}/likes/me：幂等。"""
        moment = self._load_visible_moment(viewer, moment_id)
        removed = self.store.remove_like(moment.id, viewer)
        if removed:
            self._audit(viewer, "unlike", moment.id)
        return {"moment_id": moment.id, "liked": False}

    # ------------------------------------------------- AI 内部调用（不暴露 HTTP）
    def create_moment_as_persona(
        self,
        persona_id: str,
        owner_id: str,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        """appservice 内部代发：persona 归 owner_id 校验后落库（moments.md §3）。

        payload 只含 body / media / visibility / allow_list（AI 缺省取 persona
        的 RelationDefaults.moments_visibility 映射）。
        """
        _reject_unknown(payload, ("body", "media", "visibility", "allow_list"))
        body = _require_str(payload, "body")
        assert body is not None
        if len(body) > MAX_BODY_CHARS:
            raise ValidationError(
                "request.invalid",
                f"'body' must be at most {MAX_BODY_CHARS} characters", "body",
            )
        media = _validate_media(payload.get("media"))
        allow_list = _validate_allow_list(payload.get("allow_list"))
        visibility = payload.get("visibility")
        if visibility is not None:
            visibility = _validate_visibility(visibility)

        self._require_persona_owned(persona_id, owner_id)
        relation_default = self.persona_dir.moments_visibility_default(persona_id)
        if visibility is None:
            visibility = RELATION_TO_VISIBILITY.get(relation_default, "friends")
        moment = self.store.insert_post(
            id=self._ulid(),
            author_id=persona_id,
            author_kind="ai",
            owner_id=owner_id,
            body=body,
            media=list(media),
            visibility=visibility,
            allow_list=allow_list,
            created_at=iso_utc(self._now()),
        )
        self._audit(persona_id, "create", moment.id, {"visibility": visibility})
        # 响应相对 owner（AI 无独立查看身份，friend_subject 解到主人）。
        return self._moment_response(moment, owner_id)

    def _require_persona_owned(self, persona_id: str, owner_id: str) -> None:
        owner = self.persona_dir.owner_of(persona_id)
        if owner is None or owner != owner_id:
            raise PersonaNotOwned(persona_id)

    def comment_as_persona(
        self,
        persona_id: str,
        owner_id: str,
        moment_id: str,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        """AI 评论（appservice 内部）：以 persona 名义评论，归属校验同上。

        前提与公开路径一致：moment 对该 AI 的**主人**（friend_subject）可见。
        """
        _reject_unknown(payload, ("body", "reply_to_comment_id"))
        body = _require_str(payload, "body")
        assert body is not None
        if len(body) > MAX_COMMENT_CHARS:
            raise ValidationError(
                "request.invalid",
                f"'body' must be at most {MAX_COMMENT_CHARS} characters", "body",
            )
        self._require_persona_owned(persona_id, owner_id)
        moment = self._load_visible_moment(owner_id, moment_id)
        reply_to = self._validate_reply_target(owner_id, moment, payload)
        comment = self.store.insert_comment(
            id=self._ulid(),
            moment_id=moment.id,
            author_id=persona_id,
            author_kind="ai",
            owner_id=owner_id,
            body=body,
            reply_to_comment_id=reply_to,
            created_at=iso_utc(self._now()),
        )
        self._audit(persona_id, "comment", moment.id, {"comment_id": comment.id})
        return self._comment_response(comment)

    def like_as_persona(
        self, persona_id: str, owner_id: str, moment_id: str
    ) -> Dict[str, Any]:
        """AI 点赞（appservice 内部）：归属校验后落库 actor_kind=ai。"""
        self._require_persona_owned(persona_id, owner_id)
        moment = self._load_visible_moment(owner_id, moment_id)
        created = self.store.upsert_like(
            moment_id=moment.id,
            actor_id=persona_id,
            actor_kind="ai",
            owner_id=owner_id,
            created_at=iso_utc(self._now()),
        )
        if created:
            self._audit(persona_id, "like", moment.id)
        return {"moment_id": moment.id, "liked": True}

    # ------------------------------------------------------------ 清理
    def purge(self, now: Optional[datetime] = None) -> Dict[str, int]:
        """按保留天数硬删已软删的行与过期审计（定时调用见 `start_purge_scheduler`）。"""
        current = now if now is not None else self._now()
        soft_cutoff = iso_utc(current - timedelta(days=self._soft_days))
        audit_cutoff = iso_utc(current - timedelta(days=self._audit_days))
        return self.store.purge(
            soft_delete_cutoff=soft_cutoff, audit_cutoff=audit_cutoff
        )


    def run_purge_safely(self) -> Optional[Dict[str, int]]:
        """跑一次 purge；任何异常只记日志、不外抛（不能因清理失败影响服务）。"""
        try:
            result = self.purge()
        except Exception as exc:  # noqa: BLE001 - 清理是尽力而为
            _log.warning(
                "moments purge failed: %s: %s", type(exc).__name__, exc
            )
            return None
        if any(result.values()):
            _log.info("moments purge removed %s", result)
        return result

    def start_purge_scheduler(self, interval_seconds: float) -> threading.Thread:
        """后台守护线程：每隔 interval_seconds 跑一次 `run_purge_safely`。

        首次在等待一个间隔后执行（启动时那次由 `build_moments_service`
        同步执行）。`stop_purge_scheduler()` 可让线程尽快退出。
        """
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be > 0")
        self.stop_purge_scheduler()
        stop = threading.Event()
        self._purge_stop = stop

        def _loop() -> None:
            while not stop.wait(interval_seconds):
                self.run_purge_safely()

        thread = threading.Thread(
            target=_loop, name="moments-purge", daemon=True
        )
        self._purge_thread = thread
        thread.start()
        return thread

    def stop_purge_scheduler(self, timeout: Optional[float] = 5.0) -> None:
        stop = getattr(self, "_purge_stop", None)
        thread = getattr(self, "_purge_thread", None)
        if stop is not None:
            stop.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
        self._purge_stop = None
        self._purge_thread = None


# 「allow_list 省略」的内部哨兵（PATCH 三态：省略 / null / 数组）。
_SENTINEL_KEEP = object()


def build_moments_service(
    config: Config, migrations_dir=None
) -> Optional[MomentsService]:
    """按配置装配动态服务；`GATEWAY_MOMENTS_DB_PATH` 为空 → None（模块关闭）。

    迁移目录默认仓库相对路径（`gateway/migrations/moments`），可注入（测试）。
    """
    if not config.moments_db_path:
        return None
    conn = connect(config.moments_db_path)
    apply_migrations(conn, migrations_dir)
    store = MomentStore(conn)
    relations: RelationProvider
    if config.moments_friends_file:
        relations = StaticRelationProvider.from_file(config.moments_friends_file)
    else:
        # 未配置好友图 = 空图（fail-closed：friends 档下无好友可见）。
        relations = StaticRelationProvider()
    service = MomentsService(
        store=store,
        relations=relations,
        persona_dir=EmptyPersonaDirectory(),
        server_name=config.matrix_server_name,
        idempotency_ttl_seconds=config.idempotency_ttl_seconds,
        max_idempotency_entries=config.max_idempotency_entries,
        soft_delete_retention_days=config.moments_soft_delete_retention_days,
        audit_retention_days=config.moments_audit_retention_days,
    )
    # 软删 / 审计清理（#184 审查建议）：间隔 0 = 关闭。启用时启动阶段同步跑
    # 一次（失败只记日志），之后由守护线程按间隔执行。
    interval = config.moments_purge_interval_seconds
    if interval > 0:
        service.run_purge_safely()
        service.start_purge_scheduler(interval)
    return service
