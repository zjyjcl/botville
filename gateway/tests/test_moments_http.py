#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 botville contributors
"""A2.2（issue #7）PR2 动态 HTTP 层测试（真实 GatewayServer + 桩身份校验）。

核心验收（任务书）：
  1. **越权拒绝矩阵**：4 档 visibility × 是否 allow_list 收窄 × 作者类型
     （human / AI 绑 owner）× 查看者类别 × 8 种操作，表驱动（subTest），
     断言与 moments.md §5 一致；
  2. 404 逐字节相同：同 path 同 X-Request-Id 下「不可见」与「已删除」响应
     bytes 完全相等，与「从未存在的 id」只差 instance 里的 id；
  3. 评论 / 点赞可见性（非共同好友互相看不到、AI 绑 owner、自己总能看到）；
  4. 删好友 / 改 visibility / 加 allow_list 后下一次读取立即生效；
  5. PATCH revision 冲突与 allow_list 三态；幂等（moments / comments / 点赞）；
  6. reply_to_comment_id 四种非法情况统一 422；
  7. persona 归属（403 persona.not_owned / 缺省 visibility 取 persona 默认）；
  8. 输入校验、非法 cursor / limit、非 ULID id → 404；
  9. moments 关闭（DB_PATH 空）保持旧行为（鉴权后 404/405）；
 10. 401 先于 404（无 token / 无效 token）。

user_verifier 换成按 token→user_id 的桩（不经 homeserver）；AI 评论 / 点赞
经服务层内部调用写入（不暴露 HTTP，见 test_moments_impl.py）。
运行：python3 -m unittest gateway.tests.test_moments_http -v
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from typing import Dict, List, Optional, Tuple

GATEWAY_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(GATEWAY_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from botville_gateway.config import Config  # noqa: E402
from botville_gateway.http_server import build_server  # noqa: E402
from botville_gateway.moments.ulid import new_ulid  # noqa: E402
from botville_gateway.moments.visibility import (  # noqa: E402
    StaticPersonaDirectory,
    StaticRelationProvider,
)
from botville_gateway.user_identity import (  # noqa: E402
    VERIFY_INVALID,
    VERIFY_MISSING,
    VERIFY_OK,
)

# ── 明显假身份 / 凭据（不含真实密钥） ─────────────────────────────────────
SERVER = "localhost"
ALICE = "@alice:localhost"        # human 作者
OWNER = "@owner:localhost"        # AI persona 的主人
FRIEND = "@friend:localhost"      # 作者主体的好友
STRANGER = "@stranger:localhost"  # 同服陌生人
BOB = "@bob:localhost"
CAROL = "@carol:localhost"
PERSONA = "persona_demo"

TOKENS = {
    "tok-alice-00000001": ALICE,
    "tok-owner-00000002": OWNER,
    "tok-friend-00000003": FRIEND,
    "tok-stranger-000004": STRANGER,
    "tok-bob-00000005": BOB,
    "tok-carol-00000006": CAROL,
}
USER_TOKEN = {user: token for token, user in TOKENS.items()}

_HMAC = b"test-hmac-secret-0123456789abcdef"


class StubVerifier:
    """token→user_id 的桩（两种 scheme 都认，域校验已由可见性层覆盖）。"""

    def __init__(self, mapping: Dict[str, str]) -> None:
        self.mapping = dict(mapping)

    def verify(self, header: Optional[str]) -> Tuple[str, Optional[str]]:
        if not header:
            return VERIFY_MISSING, None
        if header.startswith("MatrixOpenID "):
            token = header[len("MatrixOpenID "):].strip()
        elif header.startswith("Bearer "):
            token = header[len("Bearer "):].strip()
        else:
            return VERIFY_MISSING, None
        if not token:
            return VERIFY_MISSING, None
        user_id = self.mapping.get(token)
        if user_id is None:
            return VERIFY_INVALID, None
        return VERIFY_OK, user_id


class MomentsServer:
    """后台线程里的真实 GatewayServer（端口 0），moments 库在临时目录。"""

    def __init__(
        self,
        *,
        enabled: bool = True,
        pairs: Optional[List[List[str]]] = None,
        personas: Optional[Dict[str, tuple]] = None,
    ) -> None:
        self.tmp = tempfile.mkdtemp(prefix="moments-http-")
        overrides: Dict[str, object] = dict(
            host="127.0.0.1",
            port=0,
            dev_mode=True,
            auth_token=None,
            silent_hours_enabled=False,
            hmac_secret=_HMAC,
            matrix_server_name=SERVER,
        )
        if enabled:
            overrides["moments_db_path"] = os.path.join(self.tmp, "m.db")
        self.config = Config(**overrides)  # type: ignore[arg-type]
        self.server = build_server(self.config)
        self.server.user_verifier = StubVerifier(TOKENS)
        service = self.server.moments_service
        if service is not None:
            service.relations = StaticRelationProvider(pairs or [])
            service.persona_dir = StaticPersonaDirectory(personas or {})
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> "MomentsServer":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        if self.server.moments_service is not None:
            self.server.moments_service.stop_purge_scheduler()
            self.server.moments_service.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    @property
    def base(self) -> str:
        host, port = self.server.server_address[:2]
        return "http://{}:{}".format(host, port)

    @property
    def service(self):
        return self.server.moments_service

    # ------------------------------------------------------------ helpers
    def request(
        self,
        method: str,
        path: str,
        user: Optional[str] = None,
        *,
        body: Optional[dict] = None,
        headers: Optional[Dict[str, str]] = None,
        scheme: str = "Bearer",
    ) -> Tuple[int, Optional[dict], bytes]:
        url = self.base + path
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
        else:
            payload = None
        req = urllib.request.Request(url, data=payload, method=method)
        if user is not None:
            req.add_header("Authorization", f"{scheme} {USER_TOKEN[user]}")
        if payload is not None:
            req.add_header("Content-Type", "application/json")
        for key, value in (headers or {}).items():
            req.add_header(key, value)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read()
                status = resp.status
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            status = exc.code
        parsed = json.loads(raw) if raw else None
        return status, parsed, raw


def _create(
    fixture: MomentsServer,
    user: str,
    payload: dict,
) -> Tuple[int, dict]:
    status, body, _raw = fixture.request(
        "POST", "/v1/moments", user, body=payload
    )
    assert body is not None
    return status, body


# ============================================================================
# 1. 越权拒绝矩阵（核心验收）
# ============================================================================
VISIBILITIES = ["self", "friends", "server", "paired_servers"]
# allow 状态：None=不收窄；list_stranger=[STRANGER]（好友不在名单、陌生人在）；
# list_friend=[FRIEND]（好友在名单、陌生人不在）。
ALLOW_STATES = [None, "list_stranger", "list_friend"]
AUTHOR_KINDS = ["human", "ai"]
VIEWER_CLASSES = ["author", "friend", "stranger"]


def _allow_list(allow_state):
    if allow_state is None:
        return None
    if allow_state == "list_stranger":
        return [STRANGER]
    if allow_state == "list_friend":
        return [FRIEND]
    raise ValueError(allow_state)  # pragma: no cover


def _visible_expected(visibility: str, viewer_class: str, allow_state) -> bool:
    """moments.md §4/§5：viewer 对该动态 can_view 是否为真。"""
    if viewer_class == "author":  # human 作者本人 / AI 的所有者（短路面）
        return True
    if allow_state is None:
        in_list = True
    elif allow_state == "list_stranger":  # 只有 STRANGER 在名单内
        in_list = viewer_class == "stranger"
    else:  # list_friend：只有 FRIEND 在名单内
        in_list = viewer_class == "friend"
    if not in_list:
        return False
    if visibility == "self":
        return False
    if visibility == "friends":
        return viewer_class == "friend"
    return True  # server / paired_servers：同服登录用户（鉴权已保证）


class TestPermissionMatrix(unittest.TestCase):
    """4×3×2×3 表驱动；每格创建一条新动态后按只读→交互→改删顺序断言。"""

    def test_matrix(self):
        for visibility in VISIBILITIES:
            for allow_state in ALLOW_STATES:
                for author_kind in AUTHOR_KINDS:
                    personas = {
                        PERSONA: (OWNER, "大肥鱼", "friends"),
                    } if author_kind == "ai" else {}
                    # 好友图：human 绑 ALICE；AI 绑 OWNER（moments.md 注 0）。
                    pairs = [
                        [ALICE, FRIEND] if author_kind == "human"
                        else [OWNER, FRIEND]
                    ]
                    with MomentsServer(pairs=pairs, personas=personas) as fixture:
                        self._run_cell(
                            fixture, visibility, allow_state, author_kind
                        )

    def _run_cell(self, fixture, visibility, allow_state, author_kind):
        author_user = ALICE if author_kind == "human" else OWNER
        payload: Dict[str, object] = {
            "body": "matrix-cell",
            "visibility": visibility,
        }
        allow = _allow_list(allow_state)
        if allow is not None:
            payload["allow_list"] = allow
        if author_kind == "ai":
            payload["author_persona_id"] = PERSONA

        for viewer_class in VIEWER_CLASSES:
            with self.subTest(
                visibility=visibility, allow=allow_state,
                author_kind=author_kind, viewer=viewer_class,
            ):
                # 每类查看者用一条新动态：author 格子的 PATCH/DELETE 会改变
                # revision / 删除动态，不能污染后续格子的判定。
                status, moment = _create(fixture, author_user, payload)
                self.assertEqual(status, 201, moment)
                moment_id = moment["id"]
                viewer = author_user if viewer_class == "author" else (
                    FRIEND if viewer_class == "friend" else STRANGER
                )
                visible = _visible_expected(visibility, viewer_class, allow_state)
                privileged = viewer_class == "author"
                self._assert_cell(
                    fixture, viewer, moment_id, visibility,
                    visible, privileged,
                )

    def _assert_cell(
        self, fixture, viewer, moment_id, visibility, visible, privileged
    ):
        base = f"/v1/moments/{moment_id}"
        read = 200 if visible else 404

        # 只读操作。
        status, body, _ = fixture.request("GET", base, viewer)
        self.assertEqual(status, read, body)
        if not visible:
            self.assertEqual(body["code"], "moment.not_found")
        status, page, _ = fixture.request("GET", "/v1/moments", viewer)
        self.assertEqual(status, 200)
        listed = any(m["id"] == moment_id for m in page["items"])
        self.assertEqual(listed, visible)
        status, body, _ = fixture.request("GET", base + "/comments", viewer)
        self.assertEqual(status, read, body)
        status, body, _ = fixture.request("GET", base + "/likes", viewer)
        self.assertEqual(status, read, body)

        # 可见即可交互（评论 / 点赞）。
        status, body, _ = fixture.request(
            "PUT", base + "/likes/me", viewer
        )
        if visible:
            self.assertEqual(status, 200)
            self.assertEqual(body["liked"], True)
            self.assertEqual(body["moment_id"], moment_id)
        else:
            self.assertEqual(status, 404)
            self.assertEqual(body["code"], "moment.not_found")
        status, body, _ = fixture.request(
            "POST", base + "/comments", viewer, body={"body": "c"}
        )
        self.assertEqual(status, 201 if visible else 404, body)

        # 改 / 删：仅作者 / 所有者（可见但无权 → 同样 404，注 3）。
        status, body, _ = fixture.request(
            "PATCH", base, viewer,
            body={"revision": 1, "visibility": visibility},
        )
        self.assertEqual(status, 200 if privileged else 404, body)
        if privileged:
            self.assertEqual(body["revision"], 2)
        status, body, _ = fixture.request("DELETE", base, viewer)
        self.assertEqual(status, 204 if privileged else 404, body)

        if privileged:
            # 删除后（含作者本人）一切 404。
            status, body, _ = fixture.request("GET", base, viewer)
            self.assertEqual(status, 404)


# ============================================================================
# 2. 404 逐字节相同
# ============================================================================
class TestByteIdentical404(unittest.TestCase):
    def test_invisible_deleted_nonexistent(self):
        with MomentsServer() as fixture:
            _status, moment = _create(
                fixture, ALICE, {"body": "secret", "visibility": "self"}
            )
            moment_id = moment["id"]
            path = f"/v1/moments/{moment_id}"
            rid = {"X-Request-Id": "req-matrix-404-01"}

            # 不可见（FRIEND 看 self 档）。
            s1, b1, raw1 = fixture.request("GET", path, FRIEND, headers=rid)
            self.assertEqual(s1, 404)
            self.assertEqual(b1["code"], "moment.not_found")
            self.assertEqual(b1["title"], "Moment not found")
            self.assertEqual(
                b1["detail"],
                "Moment does not exist or is not visible to the caller; the"
                " response is byte-identical in both cases.",
            )
            self.assertEqual(b1["instance"], path)
            self.assertEqual(b1["request_id"], "req-matrix-404-01")
            self.assertEqual(
                b1["type"], "urn:botville:error:moment.not_found"
            )

            # 已删除（作者本人看已软删动态），同 path 同 X-Request-Id。
            fixture.request("DELETE", path, ALICE)
            s2, _b2, raw2 = fixture.request("GET", path, ALICE, headers=rid)
            self.assertEqual(s2, 404)
            self.assertEqual(raw1, raw2)  # 逐字节相同

            # 从未存在：只差 instance 里的 id。
            ghost = new_ulid()
            s3, b3, raw3 = fixture.request(
                "GET", f"/v1/moments/{ghost}", ALICE, headers=rid
            )
            self.assertEqual(s3, 404)
            self.assertEqual(
                raw3, raw2.replace(moment_id.encode(), ghost.encode())
            )

            # 非 ULID id 同样按 404 处理（不泄露、不区分）。
            for bad in ("not-a-ulid", "01JDYW7ZQ9GKJ4TMMB6RT8X2VTX"):
                status, body, _ = fixture.request(
                    "GET", f"/v1/moments/{bad}", ALICE
                )
                self.assertEqual(status, 404)
                self.assertEqual(body["code"], "moment.not_found")

            # 评论 / 点赞路径上的 404 与单条 404 同码同题（构造函数统一）。
            status, body, _ = fixture.request(
                "GET", f"/v1/moments/{moment_id}/comments", FRIEND
            )
            self.assertEqual((status, body["code"], body["title"]),
                             (404, "moment.not_found", "Moment not found"))


# ============================================================================
# 3. 评论 / 点赞可见性
# ============================================================================
class TestInteractionVisibility(unittest.TestCase):
    def test_mutual_friends_only(self):
        with MomentsServer(
            pairs=[[ALICE, BOB], [ALICE, CAROL]]
        ) as fixture:
            _s, moment = _create(
                fixture, ALICE, {"body": "m", "visibility": "friends"}
            )
            mid = moment["id"]
            _s, bob_comment, _ = fixture.request(
                "POST", f"/v1/moments/{mid}/comments", BOB, body={"body": "bob"}
            )
            self.assertEqual(_s, 201)
            fixture.request("PUT", f"/v1/moments/{mid}/likes/me", BOB)
            fixture.request(
                "POST", f"/v1/moments/{mid}/comments", CAROL, body={"body": "carol"}
            )
            fixture.request("PUT", f"/v1/moments/{mid}/likes/me", CAROL)

            # BOB：只看到自己的评论 / 点赞（非共同好友 CAROL 隐藏）。
            _s, bob_view, _ = fixture.request("GET", f"/v1/moments/{mid}", BOB)
            self.assertEqual(bob_view["comment_count"], 1)
            self.assertEqual(bob_view["like_count"], 1)
            self.assertTrue(bob_view["liked_by_me"])
            _s, comments, _ = fixture.request(
                "GET", f"/v1/moments/{mid}/comments", BOB
            )
            self.assertEqual([c["body"] for c in comments["items"]], ["bob"])
            _s, likes, _ = fixture.request("GET", f"/v1/moments/{mid}/likes", BOB)
            self.assertEqual([l["actor"]["id"] for l in likes["items"]], [BOB])

            # 作者看到全部。
            _s, alice_view, _ = fixture.request("GET", f"/v1/moments/{mid}", ALICE)
            self.assertEqual(alice_view["comment_count"], 2)
            self.assertEqual(alice_view["like_count"], 2)

            # server 档全可见。
            _s, server_moment = _create(
                fixture, ALICE, {"body": "s", "visibility": "server"}
            )
            sid = server_moment["id"]
            fixture.request(
                "POST", f"/v1/moments/{sid}/comments", BOB, body={"body": "b"}
            )
            fixture.request("PUT", f"/v1/moments/{sid}/likes/me", BOB)
            _s, carol_view, _ = fixture.request("GET", f"/v1/moments/{sid}", CAROL)
            self.assertEqual(carol_view["comment_count"], 1)
            self.assertEqual(carol_view["like_count"], 1)

    def test_ai_interaction_bound_to_owner(self):
        personas = {PERSONA: (OWNER, "大肥鱼", "friends")}
        with MomentsServer(
            pairs=[[ALICE, OWNER], [ALICE, BOB], [OWNER, BOB], [ALICE, CAROL]],
            personas=personas,
        ) as fixture:
            _s, moment = _create(
                fixture, ALICE, {"body": "m", "visibility": "friends"}
            )
            mid = moment["id"]
            service = fixture.service
            # AI 评论 / 点赞经内部调用写入（不暴露 HTTP）。
            service.comment_as_persona(PERSONA, OWNER, mid, {"body": "ai"})
            service.like_as_persona(PERSONA, OWNER, mid)

            # BOB（OWNER 的好友）看得到 AI 交互；CAROL（非 OWNER 好友）看不到。
            _s, bob_view, _ = fixture.request("GET", f"/v1/moments/{mid}", BOB)
            self.assertEqual(bob_view["comment_count"], 1)
            self.assertEqual(bob_view["like_count"], 1)
            _s, likes, _ = fixture.request("GET", f"/v1/moments/{mid}/likes", BOB)
            self.assertEqual(likes["items"][0]["actor"]["id"], PERSONA)
            self.assertEqual(likes["items"][0]["actor"]["kind"], "ai")
            self.assertEqual(likes["items"][0]["actor"]["owner_id"], OWNER)
            _s, carol_view, _ = fixture.request("GET", f"/v1/moments/{mid}", CAROL)
            self.assertEqual(carol_view["comment_count"], 0)
            self.assertEqual(carol_view["like_count"], 0)

            # 评论作者（其主人）总能看到自己的评论。
            _s, owner_view, _ = fixture.request("GET", f"/v1/moments/{mid}", OWNER)
            self.assertEqual(owner_view["comment_count"], 1)


# ============================================================================
# 4. 变更立即生效
# ============================================================================
class TestImmediateEffect(unittest.TestCase):
    def test_unfriend_patch_allowlist(self):
        with MomentsServer(pairs=[[ALICE, FRIEND]]) as fixture:
            _s, moment = _create(
                fixture, ALICE, {"body": "m", "visibility": "friends"}
            )
            mid = moment["id"]
            status, _b, _r = fixture.request("GET", f"/v1/moments/{mid}", FRIEND)
            self.assertEqual(status, 200)

            # 删好友：下一次读取立即生效（读取时计算）。
            fixture.service.relations.replace([])
            status, body, _ = fixture.request("GET", f"/v1/moments/{mid}", FRIEND)
            self.assertEqual(status, 404)
            _s, page, _ = fixture.request("GET", "/v1/moments", FRIEND)
            self.assertEqual(page["items"], [])

            # 加回好友 → 又可见；改 visibility=self → 立即 404。
            fixture.service.relations.replace([[ALICE, FRIEND]])
            status, _b, _r = fixture.request("GET", f"/v1/moments/{mid}", FRIEND)
            self.assertEqual(status, 200)
            fixture.request(
                "PATCH", f"/v1/moments/{mid}", ALICE,
                body={"revision": 1, "visibility": "self"},
            )
            status, _b, _r = fixture.request("GET", f"/v1/moments/{mid}", FRIEND)
            self.assertEqual(status, 404)

            # 改回 friends 并加 allow_list=[]（仅作者/所有者）→ 404；
            # null 清除 → 200。
            fixture.request(
                "PATCH", f"/v1/moments/{mid}", ALICE,
                body={"revision": 2, "visibility": "friends", "allow_list": []},
            )
            status, _b, _r = fixture.request("GET", f"/v1/moments/{mid}", FRIEND)
            self.assertEqual(status, 404)
            fixture.request(
                "PATCH", f"/v1/moments/{mid}", ALICE,
                body={"revision": 3, "allow_list": None},
            )
            status, view, _ = fixture.request("GET", f"/v1/moments/{mid}", FRIEND)
            self.assertEqual(status, 200)
            self.assertIsNone(view["allow_list"])


# ============================================================================
# 5. PATCH 语义
# ============================================================================
class TestPatchSemantics(unittest.TestCase):
    def test_revision_conflict_and_allow_states(self):
        with MomentsServer(pairs=[[ALICE, FRIEND]]) as fixture:
            _s, moment = _create(
                fixture, ALICE,
                {"body": "m", "visibility": "friends", "allow_list": [FRIEND]},
            )
            mid = moment["id"]
            # revision 冲突 → 409 moment.revision_conflict。
            status, body, _ = fixture.request(
                "PATCH", f"/v1/moments/{mid}", ALICE,
                body={"revision": 42, "visibility": "self"},
            )
            self.assertEqual(status, 409)
            self.assertEqual(body["code"], "moment.revision_conflict")
            # 省略 allow_list = 保持。
            status, body, _ = fixture.request(
                "PATCH", f"/v1/moments/{mid}", ALICE,
                body={"revision": 1, "visibility": "friends"},
            )
            self.assertEqual(status, 200)
            self.assertEqual(body["allow_list"], [FRIEND])
            self.assertEqual(body["revision"], 2)
            # [] = 仅作者/所有者。
            status, body, _ = fixture.request(
                "PATCH", f"/v1/moments/{mid}", ALICE,
                body={"revision": 2, "allow_list": []},
            )
            self.assertEqual(status, 200)
            self.assertEqual(body["allow_list"], [])
            # null = 清除。
            status, body, _ = fixture.request(
                "PATCH", f"/v1/moments/{mid}", ALICE,
                body={"revision": 3, "allow_list": None},
            )
            self.assertEqual(status, 200)
            self.assertIsNone(body["allow_list"])
            # 缺 revision / 非法 revision / 未知字段 → 400。
            for payload in (
                {"visibility": "self"},
                {"revision": 0},
                {"revision": 1, "body": "x"},
            ):
                status, body, _ = fixture.request(
                    "PATCH", f"/v1/moments/{mid}", ALICE, body=payload
                )
                self.assertEqual(status, 400, payload)

    def test_visible_but_not_author_is_404(self):
        with MomentsServer(pairs=[[ALICE, FRIEND]]) as fixture:
            _s, moment = _create(
                fixture, ALICE, {"body": "m", "visibility": "friends"}
            )
            mid = moment["id"]
            for method in ("PATCH", "DELETE"):
                status, body, _ = fixture.request(
                    method, f"/v1/moments/{mid}", FRIEND,
                    body={"revision": 1} if method == "PATCH" else None,
                )
                self.assertEqual(status, 404, method)
                self.assertEqual(body["code"], "moment.not_found")


# ============================================================================
# 6. 幂等
# ============================================================================
class TestIdempotency(unittest.TestCase):
    def test_moment_and_comment_idempotency(self):
        with MomentsServer() as fixture:
            headers = {"Idempotency-Key": "idem-key-0001"}
            s1, m1, _ = fixture.request(
                "POST", "/v1/moments", ALICE,
                body={"body": "same", "visibility": "server"}, headers=headers,
            )
            s2, m2, _ = fixture.request(
                "POST", "/v1/moments", ALICE,
                body={"body": "same", "visibility": "server"}, headers=headers,
            )
            self.assertEqual((s1, s2), (201, 201))
            self.assertEqual(m1["id"], m2["id"])  # 同一条
            # 异体 → 409 idempotency.conflict。
            s3, body, _ = fixture.request(
                "POST", "/v1/moments", ALICE,
                body={"body": "different"}, headers=headers,
            )
            self.assertEqual(s3, 409)
            self.assertEqual(body["code"], "idempotency.conflict")

            # 评论同上。
            mid = m1["id"]
            cheaders = {"Idempotency-Key": "idem-key-0002"}
            c1 = fixture.request(
                "POST", f"/v1/moments/{mid}/comments", ALICE,
                body={"body": "c"}, headers=cheaders,
            )
            c2 = fixture.request(
                "POST", f"/v1/moments/{mid}/comments", ALICE,
                body={"body": "c"}, headers=cheaders,
            )
            self.assertEqual(c1[0], 201)
            self.assertEqual(c2[0], 201)
            self.assertEqual(c1[1]["id"], c2[1]["id"])
            c3 = fixture.request(
                "POST", f"/v1/moments/{mid}/comments", ALICE,
                body={"body": "other"}, headers=cheaders,
            )
            self.assertEqual(c3[0], 409)

    def test_like_put_delete_idempotent(self):
        with MomentsServer() as fixture:
            _s, moment = _create(
                fixture, ALICE, {"body": "m", "visibility": "server"}
            )
            mid = moment["id"]
            s1, b1, _ = fixture.request("PUT", f"/v1/moments/{mid}/likes/me", BOB)
            s2, b2, _ = fixture.request("PUT", f"/v1/moments/{mid}/likes/me", BOB)
            self.assertEqual((s1, s2), (200, 200))
            self.assertEqual(b1["liked"], True)
            self.assertEqual(b2["liked"], True)
            _s, view, _ = fixture.request("GET", f"/v1/moments/{mid}", BOB)
            self.assertEqual(view["like_count"], 1)  # 不重复计数
            s3, b3, _ = fixture.request("DELETE", f"/v1/moments/{mid}/likes/me", BOB)
            s4, b4, _ = fixture.request("DELETE", f"/v1/moments/{mid}/likes/me", BOB)
            self.assertEqual((s3, s4), (200, 200))
            self.assertEqual(b3["liked"], False)
            self.assertEqual(b4["liked"], False)


# ============================================================================
# 7. reply_to_comment_id 四种非法情况
# ============================================================================
class TestReplyToInvalid(unittest.TestCase):
    def test_all_four_unified_422(self):
        with MomentsServer(
            pairs=[[ALICE, BOB], [ALICE, CAROL]]
        ) as fixture:
            _s, moment = _create(
                fixture, ALICE, {"body": "m", "visibility": "friends"}
            )
            mid = moment["id"]
            _s, bob_comment, _ = fixture.request(
                "POST", f"/v1/moments/{mid}/comments", BOB, body={"body": "b"}
            )
            hidden_id = bob_comment["id"]  # CAROL 看不到 BOB 的评论
            # 已删评论。
            fixture.request(
                "DELETE", f"/v1/moments/{mid}/comments/{hidden_id}", BOB
            )
            # 另一动态下的评论。
            _s, other = _create(
                fixture, ALICE, {"body": "o", "visibility": "friends"}
            )
            _s, other_comment, _ = fixture.request(
                "POST", f"/v1/moments/{other['id']}/comments", BOB,
                body={"body": "x"},
            )
            _s, alive, _ = fixture.request(
                "POST", f"/v1/moments/{mid}/comments", ALICE, body={"body": "a"}
            )

            # 先复活一条 BOB 的可见评论用于「不可见」用例（重新发一条）。
            _s, bob_visible, _ = fixture.request(
                "POST", f"/v1/moments/{mid}/comments", BOB, body={"body": "b2"}
            )
            cases = {
                "nonexistent": new_ulid(),
                "deleted": hidden_id,
                "other-moment": other_comment["id"],
                "invisible": bob_visible["id"],  # CAROL 与 BOB 非共同好友
                "non-ulid": "zzz-not-ulid",
            }
            for name, target in cases.items():
                with self.subTest(case=name):
                    status, body, _ = fixture.request(
                        "POST", f"/v1/moments/{mid}/comments", CAROL,
                        body={"body": "r", "reply_to_comment_id": target},
                    )
                    self.assertEqual(status, 422)
                    self.assertEqual(body["code"], "request.invalid")
                    self.assertEqual(body["errors"][0]["field"],
                                     "reply_to_comment_id")
            # 结构类型错 → 400 request.invalid（不是 422）。
            for name, target in {"int": 12345, "object": {"a": 1}, "empty": ""}.items():
                with self.subTest(type_error=name):
                    status, body, _ = fixture.request(
                        "POST", f"/v1/moments/{mid}/comments", CAROL,
                        body={"body": "r", "reply_to_comment_id": target},
                    )
                    self.assertEqual(status, 400, body)
                    self.assertEqual(body["code"], "request.invalid")
            # 合法回复目标（自己的评论 / 可见评论）→ 201。
            status, body, _ = fixture.request(
                "POST", f"/v1/moments/{mid}/comments", CAROL,
                body={"body": "r", "reply_to_comment_id": alive["id"]},
            )
            self.assertEqual(status, 201)
            self.assertEqual(body["reply_to_comment_id"], alive["id"])


# ============================================================================
# 8. persona
# ============================================================================
class TestPersonaAuthorship(unittest.TestCase):
    def test_not_owned_403(self):
        with MomentsServer() as fixture:  # 空 persona 目录（fail-closed）
            status, body, _ = fixture.request(
                "POST", "/v1/moments", ALICE,
                body={"body": "x", "author_persona_id": PERSONA},
            )
            self.assertEqual(status, 403)
            self.assertEqual(body["code"], "persona.not_owned")
            self.assertEqual(body["status"], 403)

    def test_owned_persona_default_visibility(self):
        personas = {
            PERSONA: (OWNER, "大肥鱼", "nobody"),       # nobody → self
            "persona_b": (OWNER, None, "friends"),
            "persona_c": (OWNER, None, "paired_servers"),
        }
        with MomentsServer(
            pairs=[[OWNER, FRIEND]], personas=personas
        ) as fixture:
            status, moment, _ = fixture.request(
                "POST", "/v1/moments", OWNER,
                body={"body": "x", "author_persona_id": PERSONA},
            )
            self.assertEqual(status, 201)
            self.assertEqual(moment["visibility"], "self")  # nobody → self
            self.assertEqual(moment["author"]["id"], PERSONA)
            self.assertEqual(moment["author"]["kind"], "ai")
            self.assertEqual(moment["author"]["owner_id"], OWNER)
            self.assertEqual(moment["author"]["display_name"], "大肥鱼")
            self.assertEqual(moment["owner_id"], OWNER)
            # 所有者可见；好友（OWNER 的好友）也不可见（self 档）。
            status, _b, _ = fixture.request(
                "GET", f"/v1/moments/{moment['id']}", OWNER
            )
            self.assertEqual(status, 200)
            status, _b, _ = fixture.request(
                "GET", f"/v1/moments/{moment['id']}", FRIEND
            )
            self.assertEqual(status, 404)
            # 其他默认映射。
            _s, mb, _ = fixture.request(
                "POST", "/v1/moments", OWNER,
                body={"body": "x", "author_persona_id": "persona_b"},
            )
            self.assertEqual(mb["visibility"], "friends")
            _s, mc, _ = fixture.request(
                "POST", "/v1/moments", OWNER,
                body={"body": "x", "author_persona_id": "persona_c"},
            )
            self.assertEqual(mc["visibility"], "paired_servers")
            # 他人名下的 persona → 403。
            status, body, _ = fixture.request(
                "POST", "/v1/moments", ALICE,
                body={"body": "x", "author_persona_id": "persona_b"},
            )
            self.assertEqual(status, 403)

    def test_public_comment_rejects_persona(self):
        with MomentsServer() as fixture:
            _s, moment = _create(
                fixture, ALICE, {"body": "m", "visibility": "server"}
            )
            status, body, _ = fixture.request(
                "POST", f"/v1/moments/{moment['id']}/comments", ALICE,
                body={"body": "c", "author_persona_id": PERSONA},
            )
            self.assertEqual(status, 400)  # 未知字段（additionalProperties:false）


# ============================================================================
# 9. 输入校验 / 10. 分页
# ============================================================================
class TestValidationAndPagination(unittest.TestCase):
    def test_create_validation(self):
        with MomentsServer() as fixture:
            cases = [
                ({"body": ""}, "body"),
                ({"body": "x" * 2001}, "body"),
                ({"body": "x", "unknown_field": 1}, "unknown_field"),
                ({"body": "x", "visibility": "public"}, "visibility"),
                ({"body": "x", "media": [
                    {"mxc_uri": "https://evil/x", "mimetype": "image/png"}
                ]}, "media"),
                ({"body": "x", "media": [
                    {"mxc_uri": "mxc://h/a", "mimetype": ""}
                ]}, "media"),
                ({"body": "x", "media": [
                    {"mxc_uri": "mxc://h/a", "mimetype": "image/png"}
                ] * 10}, "media"),
                ({"body": "x", "allow_list": ["@u:%s" % ("x" * 10)] * 201},
                 "allow_list"),
            ]
            for payload, field in cases:
                with self.subTest(payload=str(payload)[:60]):
                    status, body, _ = fixture.request(
                        "POST", "/v1/moments", ALICE, body=payload
                    )
                    self.assertEqual(status, 400)
                    self.assertEqual(body["errors"][0]["field"], field)
            # 合法 media 回显。
            status, moment, _ = fixture.request(
                "POST", "/v1/moments", ALICE,
                body={
                    "body": "with media",
                    "media": [{
                        "mxc_uri": "mxc://example.invalid/abc",
                        "mimetype": "image/jpeg", "width": 8, "height": 9,
                    }],
                },
            )
            self.assertEqual(status, 201)
            self.assertEqual(moment["media"][0]["mimetype"], "image/jpeg")

    def test_query_validation(self):
        with MomentsServer() as fixture:
            for query in ("limit=0", "limit=51", "limit=abc", "cursor=%40%40"):
                with self.subTest(query=query):
                    status, body, _ = fixture.request(
                        "GET", f"/v1/moments?{query}", ALICE
                    )
                    self.assertEqual(status, 400)
                    self.assertEqual(body["code"], "request.invalid")

    def test_pagination_and_author_filter(self):
        with MomentsServer() as fixture:
            created = []
            for index in range(5):
                _s, moment = _create(
                    fixture, ALICE,
                    {"body": "m%d" % index, "visibility": "server"},
                )
                created.append(moment["id"])
            # BOB 视角（同服陌生人，server 档可见）分页。
            s, page1, _ = fixture.request(
                "GET", "/v1/moments?limit=2", BOB
            )
            self.assertEqual(s, 200)
            self.assertEqual([m["id"] for m in page1["items"]],
                             list(reversed(created))[:2])
            self.assertTrue(page1["has_more"])
            self.assertIsNotNone(page1["next_cursor"])
            import urllib.parse
            cursor = urllib.parse.quote(page1["next_cursor"], safe="")
            s, page2, _ = fixture.request(
                "GET", f"/v1/moments?limit=2&cursor={cursor}", BOB
            )
            self.assertEqual([m["id"] for m in page2["items"]],
                             list(reversed(created))[2:4])
            s, page3, _ = fixture.request(
                "GET",
                f"/v1/moments?limit=2&cursor="
                + urllib.parse.quote(page2["next_cursor"], safe=""),
                BOB,
            )
            self.assertEqual([m["id"] for m in page3["items"]],
                             [created[0]])
            self.assertFalse(page3["has_more"])
            self.assertIsNone(page3["next_cursor"])
            # 列表 ⊆ GET 单条 == 200。
            for moment in page1["items"] + page2["items"] + page3["items"]:
                s, _b, _ = fixture.request(
                    "GET", f"/v1/moments/{moment['id']}", BOB
                )
                self.assertEqual(s, 200)
            # author 过滤（persona id）。
            personas = {PERSONA: (OWNER, None, "friends")}
            fixture.service.persona_dir = StaticPersonaDirectory(personas)
            _s, ai_moment, _ = fixture.request(
                "POST", "/v1/moments", OWNER,
                body={"body": "ai", "author_persona_id": PERSONA,
                      "visibility": "server"},
            )
            s, by_persona, _ = fixture.request(
                "GET", f"/v1/moments?author={PERSONA}", BOB
            )
            self.assertEqual([m["id"] for m in by_persona["items"]],
                             [ai_moment["id"]])
            s, by_alice, _ = fixture.request(
                "GET", f"/v1/moments?author={ALICE}", BOB
            )
            self.assertEqual(len(by_alice["items"]), 5)

    def test_method_routing_and_unknown_paths(self):
        with MomentsServer() as fixture:
            _s, moment = _create(
                fixture, ALICE, {"body": "m", "visibility": "server"}
            )
            mid = moment["id"]
            checks = [
                ("PUT", "/v1/moments", 405),
                ("POST", f"/v1/moments/{mid}", 405),
                ("PATCH", f"/v1/moments/{mid}/comments", 405),
                ("GET", f"/v1/moments/{mid}/comments/{mid}", 405),
                ("POST", f"/v1/moments/{mid}/likes", 405),
                ("GET", f"/v1/moments/{mid}/likes/me", 405),
                ("GET", f"/v1/moments/{mid}/unknown-sub", 404),
                ("GET", f"/v1/moments/{mid}/likes/xyz", 404),
                ("GET", f"/v1/moments/{mid}/comments/{mid}/extra", 404),
            ]
            for method, path, expected in checks:
                with self.subTest(f"{method} {path}"):
                    body = {"revision": 1} if method == "PATCH" else None
                    status, resp, _ = fixture.request(
                        method, path, ALICE, body=body
                    )
                    self.assertEqual(status, expected, resp)
                    if expected == 405:
                        self.assertEqual(resp["code"], "request.invalid")


# ============================================================================
# 11. moments 关闭 + 12. 401 先于 404
# ============================================================================
class TestDisabledAndAuth(unittest.TestCase):
    def test_disabled_keeps_placeholder_behavior(self):
        with MomentsServer(enabled=False) as fixture:
            status, body, _ = fixture.request("GET", "/v1/moments", ALICE)
            self.assertEqual(status, 404)
            self.assertEqual(body["code"], "not_found")  # 通用 404，非 moment 码
            status, body, _ = fixture.request(
                "GET", f"/v1/moments/{new_ulid()}", ALICE
            )
            self.assertEqual((status, body["code"]), (404, "not_found"))
            status, body, _ = fixture.request(
                "PATCH", f"/v1/moments/{new_ulid()}", ALICE,
                body={"revision": 1},
            )
            self.assertEqual((status, body["code"]), (405, "request.invalid"))
            # POST 也保持 404。
            status, _b, _ = fixture.request(
                "POST", "/v1/moments", ALICE, body={"body": "x"}
            )
            self.assertEqual(status, 404)

    def test_401_before_404(self):
        with MomentsServer() as fixture:
            _s, moment = _create(
                fixture, ALICE, {"body": "m", "visibility": "self"}
            )
            existing = f"/v1/moments/{moment['id']}"
            ghost = f"/v1/moments/{new_ulid()}"
            # 无 Authorization 头。
            for path in (existing, ghost, "/v1/moments"):
                status, body, _ = fixture.request("GET", path, None)
                self.assertEqual(status, 401, path)
                self.assertEqual(body["code"], "auth.missing")
            # 无效 token（桩不认识）。
            req = urllib.request.Request(fixture.base + existing, method="GET")
            req.add_header("Authorization", "Bearer bad-token-0001")
            try:
                urllib.request.urlopen(req, timeout=10)
                self.fail("expected 401")
            except urllib.error.HTTPError as exc:
                self.assertEqual(exc.code, 401)
                body = json.loads(exc.read())
                self.assertEqual(body["code"], "auth.invalid_token")
            # 未知 scheme → 401 auth.missing。
            req = urllib.request.Request(fixture.base + existing, method="GET")
            req.add_header("Authorization", "Basic dXNlcjpwYXNz")
            try:
                urllib.request.urlopen(req, timeout=10)
                self.fail("expected 401")
            except urllib.error.HTTPError as exc:
                self.assertEqual(exc.code, 401)
            # MatrixOpenID scheme 正常工作（桩同表）。
            status, _b, _ = fixture.request(
                "GET", existing, ALICE, scheme="MatrixOpenID"
            )
            self.assertEqual(status, 200)


# ============================================================================
# #184 审查 B1：幂等作用域必须含目标资源，且可见性先于幂等缓存
# ============================================================================
def _not_found(testcase, status, body):
    testcase.assertEqual(status, 404, body)
    testcase.assertEqual(body["code"], "moment.not_found")


class TestIdempotencyTargetScope(unittest.TestCase):
    KEY = "idem-key-b1-0001"

    def _key(self, key=None):
        return {"Idempotency-Key": key or self.KEY}

    def test_comment_same_key_other_visible_moment_is_independent(self):
        with MomentsServer() as fx:
            _s, a = _create(fx, ALICE, {"body": "A", "visibility": "server"})
            _s, b = _create(fx, ALICE, {"body": "B", "visibility": "server"})
            st, c1, _ = fx.request(
                "POST", f"/v1/moments/{a['id']}/comments", STRANGER,
                body={"body": "same"}, headers=self._key(),
            )
            self.assertEqual(st, 201, c1)
            st, c2, _ = fx.request(
                "POST", f"/v1/moments/{b['id']}/comments", STRANGER,
                body={"body": "same"}, headers=self._key(),
            )
            # 换目标：不得重放 A 的回执，必须真的在 B 上创建。
            self.assertEqual(st, 201, c2)
            self.assertEqual(c2["moment_id"], b["id"])
            self.assertNotEqual(c2["id"], c1["id"])
            _s, page, _ = fx.request("GET", f"/v1/moments/{b['id']}/comments", ALICE)
            self.assertEqual([c["id"] for c in page["items"]], [c2["id"]])
            # 原目标重放仍是同一回执。
            st, again, _ = fx.request(
                "POST", f"/v1/moments/{a['id']}/comments", STRANGER,
                body={"body": "same"}, headers=self._key(),
            )
            self.assertEqual(st, 201)
            self.assertEqual(again["id"], c1["id"])
            # 同目标同 key 不同请求体 → 409 idempotency.conflict（契约 §1.5）。
            st, body, _ = fx.request(
                "POST", f"/v1/moments/{a['id']}/comments", STRANGER,
                body={"body": "different"}, headers=self._key(),
            )
            self.assertEqual(st, 409, body)
            self.assertEqual(body["code"], "idempotency.conflict")

    def test_comment_key_on_unreachable_targets_is_404(self):
        with MomentsServer() as fx:
            _s, a = _create(fx, ALICE, {"body": "A", "visibility": "server"})
            _s, gone = _create(fx, ALICE, {"body": "D", "visibility": "server"})
            _s, hidden = _create(fx, ALICE, {"body": "H", "visibility": "self"})
            st, _c, _ = fx.request(
                "POST", f"/v1/moments/{a['id']}/comments", STRANGER,
                body={"body": "same"}, headers=self._key(),
            )
            self.assertEqual(st, 201)
            fx.request("DELETE", f"/v1/moments/{gone['id']}", ALICE)
            for name, mid in (
                ("nonexistent", new_ulid()),
                ("deleted", gone["id"]),
                ("invisible", hidden["id"]),
                ("non-ulid", "not-a-ulid"),
            ):
                with self.subTest(target=name):
                    st, body, _ = fx.request(
                        "POST", f"/v1/moments/{mid}/comments", STRANGER,
                        body={"body": "same"}, headers=self._key(),
                    )
                    _not_found(self, st, body)
            # 不可见目标上没有被写入任何评论。
            _s, page, _ = fx.request(
                "GET", f"/v1/moments/{hidden['id']}/comments", ALICE
            )
            self.assertEqual(page["items"], [])

    def test_comment_replay_after_delete_or_unfriend_is_404(self):
        with MomentsServer(pairs=[[ALICE, FRIEND]]) as fx:
            _s, a = _create(fx, ALICE, {"body": "A", "visibility": "server"})
            _s, f = _create(fx, ALICE, {"body": "F", "visibility": "friends"})
            for mid in (a["id"], f["id"]):
                st, _c, _ = fx.request(
                    "POST", f"/v1/moments/{mid}/comments", FRIEND,
                    body={"body": "x"}, headers=self._key(),
                )
                self.assertEqual(st, 201)
            fx.request("DELETE", f"/v1/moments/{a['id']}", ALICE)
            fx.service.relations.replace([])  # 删好友
            for mid in (a["id"], f["id"]):
                with self.subTest(moment=mid):
                    st, body, _ = fx.request(
                        "POST", f"/v1/moments/{mid}/comments", FRIEND,
                        body={"body": "x"}, headers=self._key(),
                    )
                    _not_found(self, st, body)

    def test_moment_replay_after_persona_revoked_is_403(self):
        personas = {PERSONA: (OWNER, "大肥鱼", "friends")}
        with MomentsServer(personas=personas) as fx:
            payload = {"body": "ai", "author_persona_id": PERSONA}
            st, m, _ = fx.request(
                "POST", "/v1/moments", OWNER, body=payload, headers=self._key(),
            )
            self.assertEqual(st, 201, m)
            fx.service.persona_dir = StaticPersonaDirectory({})
            st, body, _ = fx.request(
                "POST", "/v1/moments", OWNER, body=payload, headers=self._key(),
            )
            self.assertEqual(st, 403, body)
            self.assertEqual(body["code"], "persona.not_owned")

    def test_moment_and_comment_scopes_do_not_collide(self):
        with MomentsServer() as fx:
            st, m, _ = fx.request(
                "POST", "/v1/moments", ALICE,
                body={"body": "m", "visibility": "server"}, headers=self._key(),
            )
            self.assertEqual(st, 201)
            st, c, _ = fx.request(
                "POST", f"/v1/moments/{m['id']}/comments", ALICE,
                body={"body": "m"}, headers=self._key(),
            )
            self.assertEqual(st, 201, c)
            self.assertEqual(c["moment_id"], m["id"])

    def test_uncached_writes_with_key_follow_404_and_target(self):
        """点赞 / PATCH / DELETE / 删评论不做幂等缓存：带 key 也按目标独立处理。"""
        with MomentsServer() as fx:
            _s, a = _create(fx, ALICE, {"body": "A", "visibility": "server"})
            _s, b = _create(fx, ALICE, {"body": "B", "visibility": "server"})
            _s, gone = _create(fx, ALICE, {"body": "D", "visibility": "server"})
            _s, hidden = _create(fx, ALICE, {"body": "H", "visibility": "self"})
            _s, ca, _ = fx.request(
                "POST", f"/v1/moments/{a['id']}/comments", ALICE, body={"body": "c"}
            )
            fx.request("DELETE", f"/v1/moments/{gone['id']}", ALICE)
            # 同 key 点赞两条不同动态：两条都被点赞。
            for mid in (a["id"], b["id"]):
                st, state, _ = fx.request(
                    "PUT", f"/v1/moments/{mid}/likes/me", STRANGER,
                    headers=self._key(),
                )
                self.assertEqual(st, 200)
                self.assertEqual(state, {"moment_id": mid, "liked": True})
            _s, view_b, _ = fx.request("GET", f"/v1/moments/{b['id']}", STRANGER)
            self.assertTrue(view_b["liked_by_me"])
            self.assertEqual(view_b["like_count"], 1)
            for name, mid in (
                ("nonexistent", new_ulid()),
                ("deleted", gone["id"]),
                ("invisible", hidden["id"]),
            ):
                for method, path, body in (
                    ("PUT", f"/v1/moments/{mid}/likes/me", None),
                    ("DELETE", f"/v1/moments/{mid}/likes/me", None),
                    ("PATCH", f"/v1/moments/{mid}", {"revision": 1}),
                    ("DELETE", f"/v1/moments/{mid}", None),
                    ("DELETE", f"/v1/moments/{mid}/comments/{ca['id']}", None),
                ):
                    with self.subTest(target=name, op=method + " " + path):
                        st, resp, _ = fx.request(
                            method, path, STRANGER, body=body, headers=self._key(),
                        )
                        _not_found(self, st, resp)


# ============================================================================
# #184 审查 M1：外部输入解析不得 500
# ============================================================================
def _b64_cursor(payload) -> str:
    import base64
    raw = json.dumps(payload).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


class TestMalformedInputs(unittest.TestCase):
    def test_well_formed_but_invalid_cursor_is_400(self):
        with MomentsServer() as fx:
            _s, m = _create(fx, ALICE, {"body": "m", "visibility": "server"})
            _create(fx, ALICE, {"body": "n", "visibility": "server"})
            mid = m["id"]
            for user in (ALICE, STRANGER):
                fx.request("POST", f"/v1/moments/{mid}/comments", user, body={"body": "c"})
                fx.request("PUT", f"/v1/moments/{mid}/likes/me", user)
            cases = {
                "/v1/moments": [
                    {"k": "m"}, {"k": "m", "c": "x"}, {"k": "m", "i": "x"},
                    {"k": "m", "c": 1, "i": "x"}, {"k": "m", "c": "x", "i": 2},
                    {"k": "m", "c": [], "i": {}}, {"k": "m", "c": "", "i": "x"},
                    {"k": "m", "c": "x" * 600, "i": "x"}, {"k": "c", "c": "x", "i": "y"},
                    [], "str", 1, None,
                ],
                f"/v1/moments/{mid}/comments": [
                    {"k": "c"}, {"k": "c", "c": 1, "i": "x"}, {"k": "c", "c": "x", "i": None},
                    {"k": "m", "c": "x", "i": "y"},
                ],
                f"/v1/moments/{mid}/likes": [
                    {"k": "l"}, {"k": "l", "c": 1, "a": "x"}, {"k": "l", "c": "x", "i": "y"},
                    {"k": "l", "c": "x", "a": ["y"]},
                ],
            }
            for path, cursors in cases.items():
                for cursor in cursors:
                    for limit in ("1", "20"):
                        with self.subTest(path=path, cursor=cursor, limit=limit):
                            st, body, _ = fx.request(
                                "GET",
                                f"{path}?limit={limit}&cursor={_b64_cursor(cursor)}",
                                ALICE,
                            )
                            self.assertEqual(st, 400, body)
                            self.assertEqual(body["code"], "request.invalid")

    def test_lone_surrogate_in_body_is_400(self):
        surrogate = "\ud800"
        with MomentsServer() as fx:
            _s, m = _create(fx, ALICE, {"body": "m", "visibility": "server"})
            mid = m["id"]
            cases = [
                ("POST", "/v1/moments", {"body": surrogate}),
                ("POST", "/v1/moments", {"body": "ok", "media": [
                    {"mxc_uri": "mxc://h/" + surrogate, "mimetype": "image/png"}]}),
                ("POST", "/v1/moments", {"body": "ok", "media": [
                    {"mxc_uri": "mxc://h/a", "mimetype": surrogate}]}),
                ("POST", "/v1/moments", {"body": "ok", "allow_list": [surrogate]}),
                ("POST", "/v1/moments", {surrogate: "x"}),
                ("POST", f"/v1/moments/{mid}/comments", {"body": surrogate}),
                ("POST", f"/v1/moments/{mid}/comments", {surrogate: 1}),
                ("PATCH", f"/v1/moments/{mid}", {"revision": 1, "allow_list": [surrogate]}),
                ("PATCH", f"/v1/moments/{mid}", {surrogate: 1}),
            ]
            for method, path, body in cases:
                with self.subTest(method=method, path=path, body=repr(body)):
                    st, resp, _ = fx.request(method, path, ALICE, body=body)
                    self.assertEqual(st, 400, resp)
                    self.assertEqual(resp["code"], "request.invalid")

    def test_204_has_no_content_length(self):
        with MomentsServer() as fx:
            _s, m = _create(fx, ALICE, {"body": "m"})
            req = urllib.request.Request(
                fx.base + f"/v1/moments/{m['id']}", method="DELETE",
            )
            req.add_header("Authorization", "Bearer " + USER_TOKEN[ALICE])
            with urllib.request.urlopen(req, timeout=10) as resp:
                self.assertEqual(resp.status, 204)
                self.assertIsNone(resp.headers.get("Content-Length"))
                self.assertEqual(resp.read(), b"")


if __name__ == "__main__":
    unittest.main()
