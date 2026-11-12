# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 botville contributors
"""AS 事务消费骨架（B 线 PR1，contract §1.7.8）。

网关进程内的后台消费者：从 AS 事务库取「未处理、未清理、未 failed、且已到
重试时间」的事务，按插入顺序（rowid）**逐条**处理，事务内事件保持原顺序。

本 PR **不调模型、不发言**：
  - 只做事件分类、防循环判定、trust 分类、文本截断与纯函数 `should_reply`；
  - 通过钩子（`ConsumerHooks`，默认全部 no-op）把上下文交给后续 PR 的角色
    引擎 / 记忆 / 关系策略；
  - 钩子返回后才 `mark_processed`（at-least-once）；发言幂等键由
    `speech_idempotency_key(persona_user_id, event_id)` 稳定推导，重放不会重复
    发言（PR1 只提供键推导 + `SpeechDedupe` 内存实现，PR2 持久化）。

设计要点：
  - **单进程假设**与 `as_txn_store` 一致：一个网关进程一个消费者线程。
  - 唤醒：事务新落库时 `store.set_on_new_transaction()` 立即唤醒；另有 5 秒
    兜底轮询（`poll_interval` 可注入，测试用小值）。
  - 失败：普通异常 attempts+1 并按 `min(5 * 2**(attempts-1), 3600)` 退避；
    连续 5 次失败写 `failed_at`，不再取出。`TransientUnavailable`（模型 / HS
    暂时不可用）**不计 attempts**，只设 30 秒退避（同样封顶 1 小时）。
  - 损坏的事务体（不是合法 JSON / 结构不对）属**永久错误**：直接标 failed 并
    计数，不阻塞后续事务；单个事件格式不对只跳过该事件并计数。
  - 日志只记计数、耗时、异常**类型名**；永不写原始 txn_id、事件正文或 token。
  - 积压最老未处理事务超过 24 小时时记一条 warning（限频 1 小时一次，只记
    时长）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, Optional, Protocol, Sequence, Tuple

from .as_txn_store import AsTransactionStore
from .timeutil import iso_utc, utcnow

_log = logging.getLogger(__name__)

# 兜底轮询间隔（秒）。**不做环境变量**：测试通过构造参数注入更小的值。
DEFAULT_POLL_INTERVAL_SECONDS = 5.0
# 单批最多取多少条事务（与 as_txn_store 的默认批量一致）。
DEFAULT_BATCH_LIMIT = 100

# 普通失败的重试上限：连续 5 次失败后写 failed_at，不再重试。
MAX_ATTEMPTS = 5
# 指数退避基数（秒）：第 n 次失败等待 min(BASE * 2**(n-1), MAX_BACKOFF)。
BASE_BACKOFF_SECONDS = 5.0
# 瞬态不可用的退避（秒）：不计 attempts，只等这么久再试。
TRANSIENT_BACKOFF_SECONDS = 30.0
# 退避上限（秒）：1 小时。
MAX_BACKOFF_SECONDS = 3600.0

# 积压告警：最老未处理事务超过该时长记一条 warning，同一间隔内最多一条。
BACKLOG_WARN_AFTER_SECONDS = 24 * 3600
BACKLOG_WARN_INTERVAL_SECONDS = 3600

# 单条消息正文上限（Unicode 码点）。与契约 `content.text` 的 8000 字符上限一致。
MAX_TEXT_CODEPOINTS = 8000

# 本服 AI 用户 localpart 前缀（注册文件里的 exclusive user namespace `@ai_*`）。
AI_LOCALPART_PREFIX = "ai_"
# 网关自身 AS sender 的 localpart（注册模板 sender_localpart，见
# server/compose/appservice/botville-gateway.yaml.tmpl）。它发言同样不触发回复。
DEFAULT_AS_SENDER_LOCALPART = "botville-gateway"

# 正文里出现的完整 Matrix user ID（用于识别「被 @」）。只用于识别 AI 目标，
# 不做任何正文存储；匹配结果不写日志。
_USER_ID_IN_TEXT_RE = re.compile(r"@[A-Za-z0-9._=\-]+:[A-Za-z0-9.\-]+(?::[0-9]+)?")

# 事件分类（contract §1.7.8 第 7 条）。
EVENT_MESSAGE_TEXT = "message_text"
EVENT_INVITE_TO_AI = "invite_to_ai"
EVENT_REDACTION = "redaction"
EVENT_REACTION = "reaction"
EVENT_EDIT = "edit"
EVENT_OTHER_STATE = "other_state"
EVENT_OTHER = "other"

_UNTRUSTED = "untrusted"
_TRUSTED = "trusted"


class TransientUnavailable(Exception):
    """瞬态不可用（模型 / homeserver 暂时不可用）。

    钩子抛本异常时**不计入** attempts，只按短退避重试（contract §1.7.8）。
    运行时应只在本轮确实无法完成、但稍后可重试时抛出。
    """


def backoff_seconds(attempt: int) -> float:
    """第 `attempt` 次普通失败后的退避秒数：min(5 * 2**(attempt-1), 3600)。"""
    if attempt < 1:
        attempt = 1
    return min(BASE_BACKOFF_SECONDS * (2 ** (attempt - 1)), MAX_BACKOFF_SECONDS)


def transient_backoff_seconds() -> float:
    """瞬态不可用的退避秒数（固定 30 秒，封顶 1 小时）。"""
    return min(TRANSIENT_BACKOFF_SECONDS, MAX_BACKOFF_SECONDS)


def speech_idempotency_key(persona_user_id: str, event_id: str) -> str:
    """由 (persona, event_id) 稳定推导发言幂等键（contract §1.7.8 第 4 条）。

    同一 persona 对同一 Matrix 事件重复消费得到**同一个**键；不同 persona 或
    不同事件得到不同的键。键形如 `spk_<sha256 hex>`，前缀稳定、长度固定，
    可直接用作下游幂等键 / 账本键。输入不写日志。
    """
    if not isinstance(persona_user_id, str) or not isinstance(event_id, str):
        raise TypeError("persona_user_id and event_id must be strings")
    material = f"botville.speech.v1\x00{persona_user_id}\x00{event_id}".encode("utf-8")
    return "spk_" + hashlib.sha256(material).hexdigest()


def truncate_text(text: str, limit: int = MAX_TEXT_CODEPOINTS) -> str:
    """按 Unicode 码点截断正文（不是字节、不是 UTF-16 码元）。

    `limit <= 0` 返回空串；Python 字符串切片天然按码点、不拆代理对。
    """
    if not isinstance(text, str):
        return ""
    if limit <= 0:
        return ""
    return text[:limit]


def should_reply(
    *,
    sender: str,
    room_ai_user_ids: Sequence[str],
    text: str = "",
    mentions: Iterable[str] = (),
    reply_to_sender: Optional[str] = None,
    is_ai_sender: bool = False,
) -> bool:
    """纯决策函数：这条消息是否应触发 AI 回复（contract §1.7.8 第 10 条）。

    规则（与 PR2 的发言路径共用，PR1 只做判定、不发言）：

    1. `sender` 自己是 AI（`is_ai_sender` 或 sender ∈ `room_ai_user_ids`）→ 否
       （防循环；AI 互聊由 §6.3 熔断处理）。
    2. 房间里**恰好一个** AI（排除 sender）→ 是（单聊必回）。
    3. 群聊：被 **@** 时回——`mentions`（`m.mentions.user_ids`）里有该 AI，或
       正文出现该 AI 的**完整** user ID；被该 AI 回复（`reply_to_sender` 是它）
       同样回。
    4. 其余（无 AI / 多个 AI 但未被 @ / 未被回复）→ 否。

    多个 AI 同时被 @ 时由 B3 内部调度按提及顺序处理（不在本函数职责内）。
    """
    ai_ids = [uid for uid in room_ai_user_ids if uid]
    if is_ai_sender or sender in ai_ids:
        return False
    others = [uid for uid in ai_ids if uid != sender]
    if not others:
        return False
    if len(others) == 1:
        return True
    mention_set = {m for m in mentions if m}
    reply_target_is_ai = reply_to_sender is not None and reply_to_sender in others
    for uid in others:
        if uid in mention_set or uid in text:
            return True
    return reply_target_is_ai


# --------------------------------------------------------------------------- #
# 钩子与上下文
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class ConsumerContext:
    """交给钩子的上下文（不含原始请求体，日志安全）。

    字段：
      - `event_type`：分类结果（message_text / invite_to_ai / ...）；
      - `event_id` / `room_id` / `sender`：Matrix 标识；
      - `trust`：`trusted` / `untrusted`（§7.1；外服 sender 一律 untrusted）；
      - `text`：**已截断**的正文（≤ 8000 码点）；非消息事件为空串；
      - `idempotency_key`：发言幂等键；目标 AI 不明时为 None；
      - `should_reply`：纯函数判定结果；
      - `persona_user_id`：本条最相关的本服 AI（被邀请者 / 被 @ 者 / 单聊对象）。
    """

    event_type: str
    event_id: Optional[str]
    room_id: Optional[str]
    sender: Optional[str]
    trust: str
    text: str = ""
    idempotency_key: Optional[str] = None
    should_reply: bool = False
    persona_user_id: Optional[str] = None


class ConsumerHooks:
    """钩子基类：默认全部 no-op（PR1 只分类 / 路由）。

    - `on_human_text(ctx)`：人类消息（PR2 在此组装上下文、调引擎并 evaluate→send）；
    - `on_ai_text(ctx)`：AI 发出的消息（只计数，不触发回复）；
    - `on_invite_to_ai(ctx)`：邀请本服 AI 进房（PR1 no-op；关系策略接入后自动接受）；
    - `on_redaction(ctx)`：撤回事件（PR3 用于删除记忆）；
    - `on_other(ctx)`：reaction / edit / 其他 state / 未知事件（只记录）。

    钩子抛出 `TransientUnavailable` → 不计 attempts、短退避重试；其他异常 →
    attempts+1、指数退避；连续 5 次失败后该事务标 failed。
    钩子**不得**记录正文、原始 txn_id 或 token。
    """

    def on_human_text(self, ctx: ConsumerContext) -> None:  # pragma: no cover - 默认实现
        return None

    def on_ai_text(self, ctx: ConsumerContext) -> None:  # pragma: no cover
        return None

    def on_invite_to_ai(self, ctx: ConsumerContext) -> None:  # pragma: no cover
        return None

    def on_redaction(self, ctx: ConsumerContext) -> None:  # pragma: no cover
        return None

    def on_other(self, ctx: ConsumerContext) -> None:  # pragma: no cover
        return None


class SpeechDedupe(Protocol):
    """发言去重接口（PR1 内存实现；PR2 换持久化账本，语义不变）。"""

    def seen(self, key: str) -> bool:
        ...

    def mark(self, key: str) -> None:
        ...


class InMemorySpeechDedupe:
    """进程内去重（PR1；进程重启后清空，PR2 持久化到账本）。线程安全。"""

    def __init__(self) -> None:
        self._keys: set = set()
        self._lock = threading.Lock()

    def seen(self, key: str) -> bool:
        with self._lock:
            return key in self._keys

    def mark(self, key: str) -> None:
        with self._lock:
            self._keys.add(key)

    def __len__(self) -> int:
        with self._lock:
            return len(self._keys)


# --------------------------------------------------------------------------- #
# 统计
# --------------------------------------------------------------------------- #

class ConsumerStats:
    """线程安全的计数快照（只供内部 / 测试读取；**不进** /health）。

    只记录计数与累计耗时，不含任何标识或正文；错误按**异常类型名**分桶。
    """

    def __init__(self) -> None:
        self._counters: Dict[str, float] = {}
        self._lock = threading.Lock()

    def inc(self, name: str, amount: float = 1) -> None:
        with self._lock:
            self._counters[name] = self._counters.get(name, 0) + amount

    def add_seconds(self, name: str, seconds: float) -> None:
        with self._lock:
            self._counters[name] = self._counters.get(name, 0.0) + float(seconds)

    def snapshot(self) -> Dict[str, float]:
        with self._lock:
            return dict(self._counters)


# --------------------------------------------------------------------------- #
# 消费者
# --------------------------------------------------------------------------- #

class AsTransactionConsumer:
    """进程内 AS 事务消费者（daemon 线程 `as-txn-consumer`）。"""

    def __init__(
        self,
        store: AsTransactionStore,
        *,
        hooks: Optional[ConsumerHooks] = None,
        service: Any = None,
        server_name: Optional[str] = None,
        as_sender_localpart: str = DEFAULT_AS_SENDER_LOCALPART,
        dedupe: Optional[SpeechDedupe] = None,
        room_members: Optional[Callable[[str], Sequence[str]]] = None,
        conversation_resolver: Optional[Callable[[str], Optional[str]]] = None,
        now: Callable[[], datetime] = utcnow,
        poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
        batch_limit: int = DEFAULT_BATCH_LIMIT,
        max_text_codepoints: int = MAX_TEXT_CODEPOINTS,
    ) -> None:
        if poll_interval <= 0:
            raise ValueError("poll_interval must be > 0")
        if batch_limit < 1:
            raise ValueError("batch_limit must be >= 1")
        self._store = store
        self._hooks = hooks if hooks is not None else ConsumerHooks()
        self._service = service
        self._server_name = (server_name or "").lower() or None
        self._as_sender_localpart = as_sender_localpart
        self._dedupe = dedupe if dedupe is not None else InMemorySpeechDedupe()
        # 房间成员解析（PR1 没有成员同步，默认不提供 → 群聊只按 @ 判定）。
        self._room_members = room_members
        # 房间 → 会话键；默认直接用 room_id（appservice 的 `!`-前缀 room_id 在
        # 传输层同时就是合法 conversation_id）。查不到时跳过 record 并计数。
        self._conversation_resolver = conversation_resolver
        self._now = now
        self._poll_interval = poll_interval
        self._batch_limit = batch_limit
        self._max_text_codepoints = max_text_codepoints

        self._stats = ConsumerStats()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_backlog_warn: Optional[datetime] = None

    # ------------------------------------------------------------- lifecycle
    def start(self) -> threading.Thread:
        """启动后台守护线程（重复调用不会起第二个线程）。"""
        if self._thread is not None and self._thread.is_alive():
            return self._thread
        self._stop.clear()
        thread = threading.Thread(target=self._loop, name="as-txn-consumer", daemon=True)
        self._thread = thread
        thread.start()
        return thread

    def stop(self, timeout: Optional[float] = 5.0) -> None:
        """优雅关闭：置停止位并唤醒，处理完当前事务后退出。"""
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
        self._thread = None

    def wake(self) -> None:
        """立即唤醒消费者（事务落库后的回调；可重复调用）。"""
        self._wake.set()

    @property
    def running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    @property
    def stats(self) -> Dict[str, float]:
        return self._stats.snapshot()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception as exc:  # noqa: BLE001 - 消费线程不能裸抛
                # 只记异常类型名；不写 txn_id / 正文。
                self._stats.inc("loop_errors")
                _log.warning("AS consumer loop error: %s", type(exc).__name__)
            # 先清唤醒位再等待，避免「处理期间到达的唤醒」被吞掉。
            self._wake.clear()
            self._wake.wait(self._poll_interval)

    # --------------------------------------------------------------- process
    def run_once(
        self,
        *,
        now: Optional[datetime] = None,
        limit: Optional[int] = None,
    ) -> int:
        """同步处理一批事务，返回本次成功 `mark_processed` 的事务数。

        测试可直接调用；线程循环也复用它。`stop()` 后不再开始新事务。
        """
        current = now if now is not None else self._now()
        batch = self._store.get_consumable_batch(
            current, limit if limit is not None else self._batch_limit
        )
        if not batch:
            return 0
        self._stats.inc("batches")
        processed = 0
        for txn_id, body, _received_at, attempts in batch:
            if self._stop.is_set():
                break
            self._stats.inc("transactions")
            started = time.monotonic()
            try:
                self._process_transaction(txn_id, body, current, attempts)
                processed += 1
            except Exception as exc:  # noqa: BLE001 - 逐事务隔离失败
                self._handle_failure(txn_id, exc, current, attempts)
            finally:
                self._stats.add_seconds(
                    "processing_seconds", time.monotonic() - started
                )
        self._maybe_warn_backlog(current)
        return processed

    def _process_transaction(
        self,
        txn_id: str,
        body: str,
        now: datetime,
        attempts: int,
    ) -> None:
        # 处理与清理的竞争：select 后行可能刚被 purge（body 置空串）。直接标
        # processed（墓碑无害），不计失败。
        if not body or not body.strip():
            self._stats.inc("skipped_purged")
            self._store.mark_processed(txn_id)
            return
        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            self._permanent_failure(txn_id, now, "MalformedTransaction")
            return
        if not isinstance(payload, dict):
            self._permanent_failure(txn_id, now, "MalformedTransaction")
            return
        events = payload.get("events")
        if not isinstance(events, list):
            self._permanent_failure(txn_id, now, "MalformedTransaction")
            return

        for event in events:
            if self._stop.is_set():
                # 未处理完就不 mark_processed：下次从头重放（at-least-once），
                # 已执行过的钩子由幂等键去重。
                return
            self._dispatch_event(event)
        # 所有事件副作用完成后才标记（at-least-once）。
        self._store.mark_processed(txn_id)
        self._stats.inc("processed")

    def _permanent_failure(self, txn_id: str, now: datetime, error_type: str) -> None:
        """永久错误（损坏的事务体）：直接标 failed，不阻塞后续事务。"""
        self._stats.inc("malformed")
        self._stats.inc(f"errors.{error_type}")
        self._store.record_failure(
            txn_id, error_type, now, next_attempt_at=now, mark_failed=True
        )
        _log.warning("AS consumer marked transaction failed: %s", error_type)

    def _handle_failure(
        self,
        txn_id: str,
        exc: BaseException,
        now: datetime,
        attempts: int,
    ) -> None:
        if isinstance(exc, TransientUnavailable):
            delay = min(TRANSIENT_BACKOFF_SECONDS, MAX_BACKOFF_SECONDS)
            self._store.record_transient(txn_id, now + timedelta(seconds=delay))
            self._stats.inc("transient")
            self._stats.inc(f"errors.{type(exc).__name__}")
            _log.info(
                "AS consumer transient unavailability (%s); retry in %.0fs",
                type(exc).__name__,
                delay,
            )
            return
        error_type = type(exc).__name__
        attempts_new = attempts + 1
        self._stats.inc("retries")
        self._stats.inc(f"errors.{error_type}")
        if attempts_new >= MAX_ATTEMPTS:
            self._store.record_failure(
                txn_id, error_type, now, next_attempt_at=now, mark_failed=True
            )
            self._stats.inc("failed")
            _log.warning(
                "AS consumer giving up on a transaction after %d attempts: %s",
                MAX_ATTEMPTS,
                error_type,
            )
            return
        delay = min(BASE_BACKOFF_SECONDS * (2 ** (attempts_new - 1)), MAX_BACKOFF_SECONDS)
        self._store.record_failure(
            txn_id, error_type, now, next_attempt_at=now + timedelta(seconds=delay)
        )
        _log.info(
            "AS consumer transaction failed (%s), attempt %d/%d; retry in %.0fs",
            error_type,
            attempts_new,
            MAX_ATTEMPTS,
            delay,
        )

    # ---------------------------------------------------------------- events
    def _dispatch_event(self, event: Any) -> None:
        """分类单个事件并路由到钩子。格式不对只跳过该事件、计数、不抛异常。"""
        try:
            routed = self._classify_event(event)
        except Exception:  # noqa: BLE001 - 单事件格式异常安全跳过
            self._stats.inc("invalid_events")
            return
        if routed is None:
            self._stats.inc("invalid_events")
            return
        category, fields = routed
        self._stats.inc(f"event.{category}")

        sender = fields.get("sender")
        event_id = fields.get("event_id")
        room_id = fields.get("room_id")
        is_ai, trust = self._classify_sender(sender)

        if category == EVENT_MESSAGE_TEXT:
            text = truncate_text(fields.get("text") or "", self._max_text_codepoints)
            mentions = fields.get("mentions") or ()
            target = self._pick_persona_user_id(room_id, text, mentions)
            ai_ids = self._room_ai_ids(room_id, mentions)
            reply = should_reply(
                sender=sender or "",
                room_ai_user_ids=ai_ids,
                text=text,
                mentions=mentions,
                is_ai_sender=is_ai,
            )
            ctx = ConsumerContext(
                event_type=category,
                event_id=event_id,
                room_id=room_id,
                sender=sender,
                trust=trust,
                text=text,
                idempotency_key=(
                    speech_idempotency_key(target, event_id)
                    if target and event_id
                    else None
                ),
                should_reply=reply,
                persona_user_id=target,
            )
            if is_ai:
                # 防循环：AI（含网关自己的 AS sender）发言不触发回复。
                self._stats.inc("ai_messages")
                self._run_hook(self._hooks.on_ai_text, ctx)
                return
            self._stats.inc("human_messages" if trust == _TRUSTED else "untrusted_messages")
            # 人类消息重置防刷屏 / 互聊熔断计数。只对本服 trusted 人类生效：
            # 外服消息若也重置，等于让任何联邦用户清空本房间的熔断计数。
            if trust == _TRUSTED:
                self._record_human_message(room_id)
            # 去重：同一 (persona, event_id) 的发言键只执行一次钩子。
            if ctx.idempotency_key and not self._pass_dedupe(ctx.idempotency_key):
                return
            self._run_hook(self._hooks.on_human_text, ctx)
            return

        if category == EVENT_INVITE_TO_AI:
            # 只有本服 `@ai_*` 的邀请才会分类到这里（外服 `@ai_*` → other_state）。
            target = fields.get("target_user_id")
            ctx = ConsumerContext(
                event_type=category,
                event_id=event_id,
                room_id=room_id,
                sender=sender,
                trust=trust,
                persona_user_id=target,
                idempotency_key=(
                    speech_idempotency_key(target, event_id)
                    if target and event_id
                    else None
                ),
                should_reply=False,
            )
            self._stats.inc("invites")
            # 关系策略尚未接入：PR1 的 on_invite_to_ai 是 no-op stub。
            self._run_hook(self._hooks.on_invite_to_ai, ctx)
            return

        if category == EVENT_REDACTION:
            ctx = ConsumerContext(
                event_type=category,
                event_id=event_id,
                room_id=room_id,
                sender=sender,
                trust=trust,
            )
            self._stats.inc("redactions")
            self._run_hook(self._hooks.on_redaction, ctx)
            return

        # reaction / edit / 其他 state / 未知：只记录与计数。
        ctx = ConsumerContext(
            event_type=category,
            event_id=event_id,
            room_id=room_id,
            sender=sender,
            trust=trust,
        )
        self._run_hook(self._hooks.on_other, ctx)

    def _classify_event(self, event: Any) -> Optional[Tuple[str, Dict[str, Any]]]:
        """把一条 Matrix 事件分类；格式不对返回 None（调用方计数并跳过）。

        是实例方法（不是 staticmethod）：邀请目标是否算「本服 AI」依赖配置的
        server_name（`_is_local_ai`）。外服 `@ai_*` 的邀请不是 `invite_to_ai`，
        归为 `other_state`（contract §1.7.8 ⑦）。
        """
        if not isinstance(event, dict):
            return None
        event_type = event.get("type")
        if not isinstance(event_type, str) or not event_type:
            return None
        content = event.get("content")
        if content is not None and not isinstance(content, dict):
            content = {}
        content = content or {}
        fields: Dict[str, Any] = {
            "sender": event.get("sender"),
            "event_id": event.get("event_id"),
            "room_id": event.get("room_id"),
        }
        relates_to = content.get("m.relates_to")
        rel_type = relates_to.get("rel_type") if isinstance(relates_to, dict) else None

        if event_type == "m.room.message":
            if rel_type == "m.replace":
                return EVENT_EDIT, fields
            if rel_type == "m.annotation":
                return EVENT_REACTION, fields
            msgtype = content.get("msgtype")
            if msgtype in ("m.text", "m.notice"):
                fields["text"] = content.get("body")
                fields["mentions"] = _mentions_of(content)
                return EVENT_MESSAGE_TEXT, fields
            return EVENT_OTHER, fields
        if event_type == "m.reaction":
            return EVENT_REACTION, fields
        if event_type == "m.room.redaction":
            return EVENT_REDACTION, fields
        if event_type == "m.room.member":
            if content.get("membership") == "invite":
                state_key = event.get("state_key")
                if self._is_local_ai(state_key):
                    fields["target_user_id"] = state_key
                    return EVENT_INVITE_TO_AI, fields
            return EVENT_OTHER_STATE, fields
        if event_type.startswith("m.room."):
            return EVENT_OTHER_STATE, fields
        return EVENT_OTHER, fields

    # --------------------------------------------------------------- helpers
    def _classify_sender(self, sender: Optional[str]) -> Tuple[bool, str]:
        """返回 (是否 AI 侧事件, trust)。外服 sender 一律 untrusted（§7.1）。

        - localpart 以 `ai_` 开头（**含外服**）或等于网关 AS sender localpart
          （仅本服）→ AI 侧事件。外服 `@ai_*` 也按 AI 处理（conservative 防跨服
          AI 循环：不触发回复、不调 `record_human_message`），但 trust 仍是
          untrusted（contract §1.7.8 ⑦）。
        - 本服 = user ID 的 domain 等于配置的 server_name（大小写不敏感）。
          未配置 server_name 时无法确认本服 → fail-closed untrusted。
        """
        parsed = _split_user_id(sender)
        if parsed is None:
            return False, _UNTRUSTED
        localpart, domain = parsed
        if self._server_name is not None:
            is_local = domain.lower() == self._server_name
        else:
            is_local = False
        is_ai = localpart.startswith(AI_LOCALPART_PREFIX) or (
            localpart == self._as_sender_localpart and is_local
        )
        return is_ai, (_TRUSTED if is_local else _UNTRUSTED)

    def _is_local_ai(self, user_id: Optional[str]) -> bool:
        """user_id 是否是**本服** `@ai_*`（localpart 以 `ai_` 开头且 domain 等于
        配置的 server_name，大小写不敏感）。

        外服 `@ai_*` 返回 False；未配置 server_name 时 fail-closed → False。
        只用于「目标 AI」判定（邀请 state_key、被 @ 的 AI、房间 AI 成员、正文
        里的完整 ID）；sender 侧的 AI 判定见 `_classify_sender`。
        """
        parsed = _split_user_id(user_id)
        if parsed is None:
            return False
        localpart, domain = parsed
        if not localpart.startswith(AI_LOCALPART_PREFIX):
            return False
        if self._server_name is None:
            return False
        return domain.lower() == self._server_name

    def _room_ai_ids(
        self, room_id: Optional[str], mentions: Iterable[str]
    ) -> list:
        """房间里的**本服** AI 用户 ID（成员解析可选；PR1 默认只含被 @ 的本服 AI）。

        外服 `@ai_*` 不计入：contract §1.7.8 ⑦ 只认本服 AI 为「目标 AI」。
        """
        ids: list = []
        if room_id and self._room_members is not None:
            try:
                ids.extend(uid for uid in self._room_members(room_id) or ())
            except Exception:  # noqa: BLE001 - 成员解析失败不阻塞消费
                self._stats.inc("room_member_errors")
        for uid in mentions:
            if isinstance(uid, str) and self._is_local_ai(uid) and uid not in ids:
                ids.append(uid)
        return ids

    def _pick_persona_user_id(
        self, room_id: Optional[str], text: str, mentions: Iterable[str]
    ) -> Optional[str]:
        """本条消息最相关的**本服** AI：被 @ 的第一个本服 AI > 单聊房间里的唯一本服 AI > None。

        外服 `@ai_*` 不作为 persona 目标（contract §1.7.8 ⑦）。
        """
        for uid in mentions:
            if isinstance(uid, str) and self._is_local_ai(uid):
                return uid
        for match in _USER_ID_IN_TEXT_RE.findall(text):
            if self._is_local_ai(match):
                return match
        if room_id and self._room_members is not None:
            try:
                ids = [uid for uid in self._room_members(room_id) or () if self._is_local_ai(uid)]
            except Exception:  # noqa: BLE001
                ids = []
            if len(ids) == 1:
                return ids[0]
        return None

    def _record_human_message(self, room_id: Optional[str]) -> None:
        """调用 `GatewayService.record_human_message`（重置防刷屏 / 互聊熔断）。"""
        if self._service is None:
            self._stats.inc("record_skipped")
            return
        conversation_id = room_id
        if self._conversation_resolver is not None:
            conversation_id = (
                self._conversation_resolver(room_id) if room_id is not None else None
            )
        if conversation_id is None:
            self._stats.inc("record_skipped")
            return
        try:
            self._service.record_human_message(conversation_id)
            self._stats.inc("human_recorded")
        except Exception:  # noqa: BLE001 - 记录失败不能中断消费
            self._stats.inc("record_errors")

    def _pass_dedupe(self, key: str) -> bool:
        if self._dedupe.seen(key):
            self._stats.inc("dedupe_skips")
            return False
        return True

    def _run_hook(self, hook: Callable[[ConsumerContext], None], ctx: ConsumerContext) -> None:
        """执行钩子；成功后提交去重键。异常向上抛给事务级失败处理。"""
        hook(ctx)
        if ctx.idempotency_key:
            self._dedupe.mark(ctx.idempotency_key)

    def _maybe_warn_backlog(self, now: datetime) -> None:
        """积压最老未处理事务 > 24h 时记 warning（限频 1 小时，只记时长）。"""
        try:
            oldest = self._store.oldest_unprocessed_received_at()
        except Exception:  # noqa: BLE001 - 告警查询失败不影响消费
            return
        if not oldest:
            return
        try:
            received = datetime.strptime(oldest, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            return
        age = (now - received).total_seconds()
        if age < BACKLOG_WARN_AFTER_SECONDS:
            return
        last = self._last_backlog_warn
        if last is not None and (now - last).total_seconds() < BACKLOG_WARN_INTERVAL_SECONDS:
            return
        self._last_backlog_warn = now
        self._stats.inc("backlog_warnings")
        _log.warning(
            "AS consumer backlog: oldest unprocessed transaction is %.1f hours old",
            age / 3600.0,
        )


def _split_user_id(user_id: Optional[str]) -> Optional[Tuple[str, str]]:
    """`@localpart:domain` → (localpart, domain)；非 Matrix user ID 返回 None。"""
    if not isinstance(user_id, str) or not user_id.startswith("@"):
        return None
    localpart, sep, domain = user_id[1:].partition(":")
    if not sep or not localpart or not domain:
        return None
    return localpart, domain


def _mentions_of(content: Dict[str, Any]) -> Tuple[str, ...]:
    """读取 `m.mentions.user_ids`（MSC3952）；结构不对时返回空。"""
    mentions = content.get("m.mentions")
    if not isinstance(mentions, dict):
        return ()
    ids = mentions.get("user_ids")
    if not isinstance(ids, list):
        return ()
    return tuple(uid for uid in ids if isinstance(uid, str))
