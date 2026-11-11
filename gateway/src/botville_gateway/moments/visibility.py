# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 botville contributors
"""动态可见性判定（moments.md §4，单一函数供所有读路径复用）。

- `friend_subject`：把行为者（作者 / 评论者 / 点赞者）解到 Matrix user——
  AI persona 绑到 `owner_id`，human 即自身 id。好友判定永远是
  Matrix user ↔ Matrix user。
- `can_view(viewer, moment)`：读单条 / 列表 / 评论 / 点赞 / 计数共用的
  可见性函数；软删 = 不存在，越权与不存在在服务层统一 404。
- `can_see_interaction(viewer, moment, actor)`：单条评论 / 点赞在
  `can_view` 之上的第二层可见性（类朋友圈：非共同好友互相看不到）。

`RelationProvider` 是好友图接口（A2.3 落地后替换实现，调用点不变）；
`PersonaDirectory` 是 persona 归属 / 展示名 / 缺省可见范围接口
（PR2 提供可注入的静态实现，默认空目录 fail-closed）。
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Dict, Iterable, Optional, Sequence, Tuple

from ..config import ConfigError
from .store import CommentRow, LikeRow, MomentRow

# RelationDefaults.moments_visibility（nobody/friends/paired_servers）→
# 动态可见档位 Visibility（moments.md §2）。
RELATION_TO_VISIBILITY = {
    "nobody": "self",
    "friends": "friends",
    "paired_servers": "paired_servers",
}


class RelationProvider:
    """好友图接口（moments.md §4）。两端均为 Matrix user id，判定对称。"""

    def is_friend(self, a: str, b: str) -> bool:  # pragma: no cover - 接口
        raise NotImplementedError


class StaticRelationProvider(RelationProvider):
    """临时好友图（PR2）：配置文件加载或测试直接注入，A2.3 后替换。

    图为无向边集合；`replace` 允许测试在运行中整图替换（「删好友后下一次
    读取立即生效」——可见性在读取时计算，不存快照）。线程安全。
    """

    def __init__(self, pairs: Iterable[Sequence[str]] = ()) -> None:
        self._lock = threading.Lock()
        self._edges: set = set()
        self.replace(pairs)

    @staticmethod
    def _normalize(pairs: Iterable[Sequence[str]]) -> set:
        edges = set()
        for pair in pairs:
            if len(pair) != 2:
                raise ConfigError(
                    "friends file entries must be 2-element [a, b] pairs"
                )
            a, b = pair
            if not isinstance(a, str) or not isinstance(b, str) or not a or not b:
                raise ConfigError("friends file entries must be non-empty strings")
            edges.add((a, b) if a <= b else (b, a))
        return edges

    def replace(self, pairs: Iterable[Sequence[str]]) -> None:
        """整图替换（测试 / 热更新用）。"""
        edges = self._normalize(pairs)
        with self._lock:
            self._edges = edges

    @classmethod
    def from_file(cls, path: str) -> "StaticRelationProvider":
        """从 JSON 文件加载：`{"friends": [["@a:hs","@b:hs"], ...]}`。

        文件缺失 / 结构非法 / 条目畸形 → ConfigError（启动即失败，
        fail-closed，不静默变成空图）。
        """
        file_path = Path(path)
        try:
            raw = file_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ConfigError(
                f"GATEWAY_MOMENTS_FRIENDS_FILE could not be read: {path!r}"
            ) from exc
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise ConfigError(
                f"GATEWAY_MOMENTS_FRIENDS_FILE is not valid JSON: {path!r}"
            ) from exc
        if not isinstance(data, dict) or not isinstance(data.get("friends"), list):
            raise ConfigError(
                "GATEWAY_MOMENTS_FRIENDS_FILE must be a JSON object with a"
                " 'friends' array of [a, b] pairs"
            )
        try:
            return cls(data["friends"])
        except ConfigError as exc:
            raise ConfigError(f"GATEWAY_MOMENTS_FRIENDS_FILE: {exc}") from exc

    def is_friend(self, a: str, b: str) -> bool:
        key = (a, b) if a <= b else (b, a)
        with self._lock:
            return key in self._edges


class PersonaDirectory:
    """persona 目录接口（moments.md §3）。PR2 默认实现 fail-closed。"""

    def owner_of(self, persona_id: str) -> Optional[str]:
        """persona 的主人 Matrix user id；未知 persona → None（403 路径）。"""
        raise NotImplementedError  # pragma: no cover - 接口

    def display_name(self, persona_id: str) -> Optional[str]:
        """persona 展示名快照（响应 MomentAuthor.display_name）；无则 None。"""
        raise NotImplementedError  # pragma: no cover - 接口

    def moments_visibility_default(self, persona_id: str) -> str:
        """该 persona 的 RelationDefaults.moments_visibility 原始值
        （nobody / friends / paired_servers，映射在服务层做）。"""
        raise NotImplementedError  # pragma: no cover - 接口


class EmptyPersonaDirectory(PersonaDirectory):
    """空目录（fail-closed）：任何 persona 都不归任何人 → 403 persona.not_owned。

    PR2 未接 appservice persona 存储；测试可注入 StaticPersonaDirectory。
    """

    def owner_of(self, persona_id: str) -> Optional[str]:
        return None

    def display_name(self, persona_id: str) -> Optional[str]:
        return None

    def moments_visibility_default(self, persona_id: str) -> str:
        return "friends"


class StaticPersonaDirectory(PersonaDirectory):
    """可注入的静态 persona 目录（测试 / 临时部署用）。"""

    def __init__(
        self,
        personas: Optional[Dict[str, Tuple[str, Optional[str], str]]] = None,
    ) -> None:
        """personas: {persona_id: (owner_id, display_name, moments_visibility)}。"""
        self._personas = dict(personas or {})

    def owner_of(self, persona_id: str) -> Optional[str]:
        entry = self._personas.get(persona_id)
        return entry[0] if entry else None

    def display_name(self, persona_id: str) -> Optional[str]:
        entry = self._personas.get(persona_id)
        return entry[1] if entry else None

    def moments_visibility_default(self, persona_id: str) -> str:
        entry = self._personas.get(persona_id)
        if not entry:
            return "friends"
        return entry[2] if entry[2] in RELATION_TO_VISIBILITY else "friends"


# --------------------------------------------------------------- 判定函数

def friend_subject(kind: str, entity_id: str, owner_id: str) -> str:
    """行为者 → 好友判定主体：AI 绑主人 owner_id，human 即自身 id。"""
    return owner_id if kind == "ai" else entity_id


def _same_server_viewer(viewer: str, server_name: Optional[str]) -> bool:
    """viewer 是否本服用户（moments.md §4 server 档）。

    「已通过 whoami」由 `/moments*` 鉴权层保证（两种凭据都做域校验）；
    这里再比对一次域作为纵深防御。server_name 未知 → fail-closed。
    """
    if not server_name or not viewer.startswith("@"):
        return False
    _localpart, sep, server = viewer[1:].partition(":")
    if not sep or not server:
        return False
    return server.lower() == server_name.lower()


def can_view(
    viewer: Optional[str],
    moment: MomentRow,
    relations: RelationProvider,
    *,
    server_name: Optional[str],
) -> bool:
    """动态对查看者是否可见（moments.md §4，逐行对应）。

    viewer 恒为 Matrix user id（whoami / 跨服担保），绝不是 persona id。
    """
    if moment.deleted_at is not None:
        return False  # 软删 = 对外不存在
    if viewer is None:
        return False  # 匿名 / 无效 token（层面前置 401，防御性兜底）
    if viewer == moment.author_id or viewer == moment.owner_id:
        return True   # 作者 / 所有者短路面，allow_list 不适用
    if moment.visibility == "self":
        return False
    if moment.allow_list is not None and viewer not in moment.allow_list:
        return False  # 只收窄，不放宽；None（null/省略）= 不收窄
    if moment.visibility == "friends":
        return relations.is_friend(
            viewer,
            friend_subject(moment.author_kind, moment.author_id, moment.owner_id),
        )
    if moment.visibility == "server":
        return _same_server_viewer(viewer, server_name)
    if moment.visibility == "paired_servers":
        # PR2 只实现同服查看者；「viewer 域 ∈ active 配对表」归 PR3。
        return _same_server_viewer(viewer, server_name)
    return False


def can_see_interaction(
    viewer: Optional[str],
    moment: MomentRow,
    actor_kind: str,
    actor_id: str,
    actor_owner_id: str,
    relations: RelationProvider,
) -> bool:
    """单条评论 / 点赞对查看者是否可见（moments.md §4）。

    前提 `can_view(viewer, moment)` 已由调用方成立；本函数只判第二层。
    """
    if viewer is None:
        return False
    if viewer == moment.author_id or viewer == moment.owner_id:
        return True  # 动态作者 / 所有者
    if viewer == actor_id or viewer == friend_subject(
        actor_kind, actor_id, actor_owner_id
    ):
        return True  # 查看者就是该评论 / 点赞者（或其主人）
    if moment.visibility in ("server", "paired_servers"):
        return True  # 动态本身全服（或跨服）可见时交互跟随
    return relations.is_friend(
        viewer, friend_subject(actor_kind, actor_id, actor_owner_id)
    )


def comment_visible(
    viewer: Optional[str],
    moment: MomentRow,
    comment: CommentRow,
    relations: RelationProvider,
    *,
    server_name: Optional[str],
) -> bool:
    """can_view ∧ can_see_interaction ∧ 评论未软删（列表 / 计数 / 回复目标共用）。"""
    if comment.deleted_at is not None:
        return False
    if not can_view(viewer, moment, relations, server_name=server_name):
        return False
    return can_see_interaction(
        viewer, moment, comment.author_kind, comment.author_id,
        comment.owner_id, relations,
    )


def like_visible(
    viewer: Optional[str],
    moment: MomentRow,
    like: LikeRow,
    relations: RelationProvider,
    *,
    server_name: Optional[str],
) -> bool:
    if not can_view(viewer, moment, relations, server_name=server_name):
        return False
    return can_see_interaction(
        viewer, moment, like.actor_kind, like.actor_id, like.owner_id, relations,
    )
