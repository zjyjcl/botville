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
from collections import OrderedDict
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


class MemoryDBUnavailable(TransientUnavailable):
    """Memory failure with a bounded retry hint; carries no database contents."""

    def __init__(self, retry_seconds: float) -> None:
        super().__init__("memory database unavailable")
        self.retry_seconds = retry_seconds


class DeferredUntil(TransientUnavailable):
    """策略暂不可用；在指定窗口结束时重试，不消耗 attempts。"""

    def __init__(self, retry_at: datetime) -> None:
        super().__init__("policy deferred until window end")
        self.retry_at = retry_at


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
    room_is_dm: bool = False,
) -> bool:
    """纯决策函数：这条消息是否应触发 AI 回复（contract §1.7.8 第 10 条）。

    规则（与 PR2 的发言路径共用）：

    1. `sender` 自己是 AI（`is_ai_sender` 或 sender ∈ `room_ai_user_ids`）→ 否
       （防循环；AI 互聊由 §6.3 熔断处理）。
    2. 仅已确认的 DM（`room_is_dm`）且有唯一 AI → 是（单聊必回）。
    3. 群聊：被 **@** 时回——`mentions`（`m.mentions.user_ids`）里有该 AI，或
       正文出现该 AI 的**完整** user ID；或回复该 AI 的消息时回。
    4. 其余（无 AI / 多个 AI 但未被 @ / 未被回复）→ 否。

    `reply_to_sender` 是**被回复事件**（`m.in_reply_to.event_id` 指向的事件）的
    sender，由调用方解析（进程内缓存 + 可选回填 fetcher，见 `EventSenderCache`）；
    解析不出来时传 None（fail-closed = 不是回复）。

    多个 AI 同时被 @ 时由内部调度按提及顺序逐个处理（不在本函数职责内）。
    """
    ai_ids = [uid for uid in room_ai_user_ids if uid]
    if is_ai_sender or sender in ai_ids:
        return False
    others = [uid for uid in ai_ids if uid != sender]
    if not others:
        return False
    if room_is_dm and len(others) == 1:
        return True
    mention_set = {m for m in mentions if m}
    reply_target_is_ai = reply_to_sender is not None and reply_to_sender in others
    for uid in others:
        if uid in mention_set or _has_full_user_id(text, uid):
            return True
    return reply_target_is_ai


def reply_to_event_id(content: Any) -> Optional[str]:
    """读取 `content["m.relates_to"]["m.in_reply_to"]["event_id"]`。

    只认**非空字符串**：结构不对 / 类型不对 / 空串一律返回 None（fail-closed，
    调用方据此按「不是回复」处理）。不记录正文、不写日志。
    """
    if not isinstance(content, dict):
        return None
    relates_to = content.get("m.relates_to")
    if not isinstance(relates_to, dict):
        return None
    in_reply_to = relates_to.get("m.in_reply_to")
    if not isinstance(in_reply_to, dict):
        return None
    event_id = in_reply_to.get("event_id")
    if not isinstance(event_id, str) or not event_id:
        return None
    return event_id


# 被回复事件 sender 缓存的默认容量（条）。只存 event_id → sender，不存正文。
DEFAULT_SENDER_CACHE_ENTRIES = 2048


class EventSenderCache:
    """有界 LRU：`event_id → sender`（进程内，线程安全）。

    判定「是否被某个本服 AI 回复」需要知道**被回复那条事件**的 sender。缓存由
    消费者在处理**每一条**成功分类的事件时写入（`remember()`）：
      - 同一事务内先出现的目标事件必然命中；
      - 更早事务 / 重启后的目标事件不在缓存里 → 调用方可选地走 AS 传输的
        回填 fetcher（`_reply_target_sender`）；
      - fetcher 失败 / 超时 / 查不到 → 一律当作「不是回复」（fail-closed）。

    只保存 (event_id, sender) 两个标识：不存正文、房间或时间；不写日志、
    不进 `/health`。容量满时按 LRU 淘汰最久未使用的一条。
    """

    def __init__(self, max_entries: int = DEFAULT_SENDER_CACHE_ENTRIES) -> None:
        if not isinstance(max_entries, int) or isinstance(max_entries, bool):
            raise ValueError("max_entries must be an integer")
        if max_entries < 1:
            raise ValueError("max_entries must be >= 1")
        self._max = max_entries
        self._items: "OrderedDict[str, str]" = OrderedDict()
        self._lock = threading.Lock()

    def remember(self, event_id: Any, sender: Any) -> None:
        """记录一条事件的 sender（非字符串 / 空的标识直接忽略）。"""
        if not isinstance(event_id, str) or not event_id:
            return
        if not isinstance(sender, str) or not sender:
            return
        with self._lock:
            self._items[event_id] = sender
            self._items.move_to_end(event_id)
            while len(self._items) > self._max:
                self._items.popitem(last=False)

    def get(self, event_id: Any) -> Optional[str]:
        """查 sender；未命中返回 None。命中时刷新 LRU 位置。"""
        if not isinstance(event_id, str) or not event_id:
            return None
        with self._lock:
            sender = self._items.get(event_id)
            if sender is not None:
                self._items.move_to_end(event_id)
            return sender

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)



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
      - `should_reply`：是否应触发任意一个本服 AI 回复；
      - `persona_user_id`：本条最相关的本服 AI（首要目标）；
      - `target_user_ids`：**按提及顺序排列**的全部目标本服 AI（群聊多人 @）；
        单聊时含唯一 AI、空列表时不回复。PR2 按此列表逐个发言；
      - `reply_to_event_id`：被回复事件的 event_id（`m.in_reply_to`，仅消息事件）。
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
    target_user_ids: Tuple[str, ...] = ()
    reply_to_event_id: Optional[str] = None
    # For m.room.redaction only, the event being removed.  It is deliberately
    # separate from event_id (the redaction event itself).
    redacts_event_id: Optional[str] = None
    # Local AI members known when routing this event; used solely to choose
    # memory DM/group scope.  It contains no message body.
    room_ai_user_ids: Tuple[str, ...] = ()
    # True only when membership lookup (not a mention fallback) confirmed the
    # room's local-AI membership.  Memory must never turn a guessed DM into a
    # DM scope.
    room_members_confirmed: bool = False
    # True only for a confirmed room containing exactly this persona and one
    # trusted local human.  Every other room, including lookup failures, is a
    # group for routing and memory purposes.
    room_is_dm: bool = False
    mentioned_user_ids: Tuple[str, ...] = ()
    invite_encrypted: bool = False
    encryption_state: bool = False
    membership: Optional[str] = None


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

    def set(self, name: str, value: float) -> None:
        with self._lock:
            self._counters[name] = value

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
        room_member_observer: Optional[Callable[[Any], None]] = None,
        conversation_resolver: Optional[Callable[[str], Optional[str]]] = None,
        sender_cache: Optional[EventSenderCache] = None,
        sender_fetcher: Optional[Callable[[str, str], Optional[str]]] = None,
        now: Callable[[], datetime] = utcnow,
        poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
        batch_limit: int = DEFAULT_BATCH_LIMIT,
        max_text_codepoints: int = MAX_TEXT_CODEPOINTS,
        group_max_responders: int = 3,
        persona_user_ids: Sequence[str] = (),
    ) -> None:
        if poll_interval <= 0:
            raise ValueError("poll_interval must be > 0")
        if batch_limit < 1:
            raise ValueError("batch_limit must be >= 1")
        if not 1 <= group_max_responders <= 20:
            raise ValueError("group_max_responders must be between 1 and 20")
        self._store = store
        self._hooks = hooks if hooks is not None else ConsumerHooks()
        self._service = service
        self._server_name = (server_name or "").lower() or None
        self._as_sender_localpart = as_sender_localpart
        self._dedupe = dedupe if dedupe is not None else InMemorySpeechDedupe()
        # 房间成员解析（PR1 没有成员同步，默认不提供 → 群聊只按 @ 判定）。
        self._room_members = room_members
        self._room_member_observer = room_member_observer
        # 房间 → 会话键；默认直接用 room_id（appservice 的 `!`-前缀 room_id 在
        # 传输层同时就是合法 conversation_id）。查不到时跳过 record 并计数。
        self._conversation_resolver = conversation_resolver
        # 被回复事件的 sender 解析：进程内有界 LRU（每条消费过的事件都写入）
        # + 可选回填 fetcher（`(room_id, event_id) -> sender | None`，通常实现为
        # AS 传输 `GET /_matrix/client/v3/rooms/{roomId}/event/{eventId}`）。
        # fetcher 出错 / 超时 / 查不到一律按「不是回复」处理（fail-closed）。
        self._sender_cache = (
            sender_cache if sender_cache is not None else EventSenderCache()
        )
        self._sender_fetcher = sender_fetcher
        self._now = now
        self._poll_interval = poll_interval
        self._batch_limit = batch_limit
        self._max_text_codepoints = max_text_codepoints
        self._group_max_responders = group_max_responders
        self._persona_user_ids = tuple(
            user_id for user_id in persona_user_ids if self._is_local_ai(user_id)
        )

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
            # 在开始处理**之前**清唤醒位：处理期间到达的 wake() 会重新置位，
            # 下面的 wait 立刻返回、再来一轮（否则那一轮唤醒会被吞掉，最坏等满
            # 一个 poll_interval）。唤醒是「计数器语义」而非「一次性事件」。
            self._wake.clear()
            try:
                self.run_once()
            except Exception as exc:  # noqa: BLE001 - 消费线程不能裸抛
                # 只记异常类型名；不写 txn_id / 正文。
                self._stats.inc("loop_errors")
                _log.warning("AS consumer loop error: %s", type(exc).__name__)
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
            self._store.mark_processed(txn_id, now=now)
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
        self._store.mark_processed(txn_id, now=now)
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
        if isinstance(exc, DeferredUntil):
            self._store.record_transient(txn_id, exc.retry_at)
            self._stats.inc("deferred")
            return
        if isinstance(exc, TransientUnavailable):
            delay = TRANSIENT_BACKOFF_SECONDS
            if isinstance(exc, MemoryDBUnavailable):
                delay = max(delay, exc.retry_seconds)
            delay = min(delay, MAX_BACKOFF_SECONDS)
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
        event_id = fields.get("event_id")
        # 事件标识必须是**非空字符串**：缺失 / 非字符串（数字、null、对象）
        # 只跳过**这一条**并计数，不把整条事务标 failed（contract §1.7.8 ⑤：
        # 单事件格式不对只跳过该事件，不影响事务内其他事件）。没有 event_id
        # 也无法推导发言幂等键，继续处理会绕过去重。
        if not isinstance(event_id, str) or not event_id:
            self._stats.inc("invalid_events")
            self._stats.inc("invalid_event_ids")
            return
        self._stats.inc(f"event.{category}")
        if event.get("type") == "m.room.member":
            content = event.get("content")
            persona = event.get("state_key")
            membership = content.get("membership") if isinstance(content, dict) else None
            # A malformed member event (non-object content, non-string
            # membership or state_key) skips only this event; it must not
            # fail the whole transaction or touch the invite ledger.
            if not isinstance(persona, str) or not isinstance(membership, str):
                self._stats.inc("invalid_events")
                self._stats.inc("invalid_member_events")
                return
            room = fields.get("room_id")
            if isinstance(persona, str) and self._is_local_ai(persona) and isinstance(room, str) and room.startswith("!"):
                unsigned = event.get("unsigned")
                stripped = unsigned.get("invite_room_state", []) if isinstance(unsigned, dict) else []
                encrypted = not isinstance(stripped, list) or any(
                    isinstance(state, dict) and state.get("type") == "m.room.encryption"
                    for state in (stripped if isinstance(stripped, list) else [])
                )
                # Storage failures must retry the transaction, not silently retain
                # a stale invitation after leave/ban.
                self._store.invitation_update(persona, room, membership, encrypted, event_id)

        if event.get("type") == "m.room.member" and self._room_member_observer is not None:
            try:
                self._room_member_observer(event)
            except Exception:
                self._stats.inc("room_member_errors")

        sender = fields.get("sender")
        room_id = fields.get("room_id")
        is_ai, trust = self._classify_sender(sender)

        # 每条被接受的事件都进 sender 缓存：后续事务/同一事务内「回复某人」
        # 的判定据此找到被回复事件的 sender（不缓存正文）。
        self._sender_cache.remember(event_id, sender)

        if category == EVENT_MESSAGE_TEXT:
            text = truncate_text(fields.get("text") or "", self._max_text_codepoints)
            mentions = fields.get("mentions") or ()
            reply_event_id = fields.get("reply_to_event_id")
            reply_to_sender = self._reply_target_sender(reply_event_id, room_id)
            room_ai_user_ids, room_members_confirmed, room_is_dm, dm_human_user_id = self._confirmed_room_ai_ids(room_id)
            # A stale/foreign event sender must not inherit the private DM
            # scope merely because the current membership snapshot is a pair.
            room_is_dm = room_is_dm and (is_ai or sender == dm_human_user_id)
            reply_ai_ids = list(room_ai_user_ids)
            # A failed member lookup is group/fail-closed, but it must still
            # permit an explicit full-ID mention or reply to a configured
            # local persona.
            if not room_is_dm:
                for persona_id in self._persona_user_ids:
                    if persona_id not in reply_ai_ids:
                        reply_ai_ids.append(persona_id)
            if not room_is_dm:
                for mentioned in mentions:
                    if self._is_local_ai(mentioned) and mentioned not in reply_ai_ids:
                        reply_ai_ids.append(mentioned)
            targets = self._reply_targets(
                room_id=room_id,
                text=text,
                mentions=mentions,
                sender=sender or "",
                reply_to_sender=reply_to_sender,
                is_ai_sender=is_ai,
                ai_ids=reply_ai_ids,
                room_is_dm=room_is_dm,
            )
            if not room_is_dm and len(targets) > self._group_max_responders:
                self._stats.inc("group_responder_skips", len(targets) - self._group_max_responders)
                targets = targets[:self._group_max_responders]
            target = targets[0] if targets else None
            reply = bool(targets)
            ctx = ConsumerContext(
                event_type=category,
                event_id=event_id,
                room_id=room_id,
                sender=sender,
                trust=trust,
                text=text,
                idempotency_key=(
                    speech_idempotency_key(target, event_id) if target else None
                ),
                should_reply=reply,
                persona_user_id=target,
                target_user_ids=targets,
                reply_to_event_id=reply_event_id,
                room_ai_user_ids=tuple(room_ai_user_ids),
                room_members_confirmed=room_members_confirmed,
                room_is_dm=room_is_dm,
                mentioned_user_ids=tuple(uid for uid in mentions if isinstance(uid, str)),
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
            # 引擎按每个 persona 的成功回执去重，不能以首个目标跳过整条事件。
            if getattr(self._hooks, "manages_speech_dedupe", False):
                self._hooks.on_human_text(ctx)
                return
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
                invite_encrypted=fields.get("invite_encrypted", False),
                idempotency_key=(
                    speech_idempotency_key(target, event_id)
                    if target and event_id
                    else None
                ),
                should_reply=False,
            )
            self._stats.inc("invites")
            # Deployment opt-in is enforced by the invite hook.
            self._run_hook(self._hooks.on_invite_to_ai, ctx)
            return

        if category == EVENT_REDACTION:
            ctx = ConsumerContext(
                event_type=category,
                event_id=event_id,
                room_id=room_id,
                sender=sender,
                trust=trust,
                redacts_event_id=fields.get("redacts_event_id"),
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
            encryption_state=fields.get("encryption_state", False),
            membership=fields.get("membership"),
            persona_user_id=fields.get("target_user_id"),
        )
        if ctx.encryption_state or ctx.membership:
            # A failed durable disable must retry the transaction, not discard
            # the only notification that this room became encrypted.
            self._hooks.on_other(ctx)
        else:
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
                # 回复关系：`m.relates_to.m.in_reply_to.event_id`（只认非空字符串）。
                fields["reply_to_event_id"] = reply_to_event_id(content)
                return EVENT_MESSAGE_TEXT, fields
            return EVENT_OTHER, fields
        if event_type == "m.reaction":
            return EVENT_REACTION, fields
        if event_type == "m.room.redaction":
            # Matrix v1 permits the old top-level field; newer events carry it
            # in content.  Accept either without recording either identifier.
            redacts = content.get("redacts") or event.get("redacts")
            if isinstance(redacts, str) and redacts:
                fields["redacts_event_id"] = redacts
            return EVENT_REDACTION, fields
        if event_type == "m.room.member":
            if content.get("membership") in ("leave", "ban") and self._is_local_ai(event.get("state_key")):
                fields["membership"] = content["membership"]
                fields["target_user_id"] = event["state_key"]
            if content.get("membership") == "invite":
                state_key = event.get("state_key")
                if self._is_local_ai(state_key):
                    fields["target_user_id"] = state_key
                    unsigned = event.get("unsigned")
                    stripped = unsigned.get("invite_room_state", []) if isinstance(unsigned, dict) else []
                    fields["invite_encrypted"] = (
                        not isinstance(stripped, list) or any(
                            isinstance(item, dict) and item.get("type") == "m.room.encryption"
                            for item in stripped))
                    return EVENT_INVITE_TO_AI, fields
            return EVENT_OTHER_STATE, fields
        if event_type == "m.room.encryption" and event.get("state_key") == "":
            fields["encryption_state"] = True
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
        """房间里**本服** `@ai_*` 用户 ID，按「被 @ 的先后 + 成员顺序」去重。

        `room_members(room_id)` 的返回值**必须过滤**：成员回调可能把人类、外服
        用户乃至非 `@ai_*` 的账号一起返回，直接采信会让 `should_reply` 的
        「单聊必回 / 群聊需 @」判错目标。这里只保留 `_is_local_ai` 为真的
        （domain == `GATEWAY_MATRIX_SERVER_NAME` 且 localpart 以 `ai_` 开头）。

        fail-closed：未配置 server_name → 没有任何可识别的本服 AI；回调抛异常
        → 只用 mentions（同样逐个过滤），并计 `room_member_errors`。
        """
        ids: list = []
        if room_id and self._room_members is not None:
            try:
                for uid in self._room_members(room_id) or ():
                    if self._is_local_ai(uid) and uid not in ids:
                        ids.append(uid)
            except Exception:  # noqa: BLE001 - 成员解析失败不阻塞消费
                self._stats.inc("room_member_errors")
        for uid in mentions:
            if isinstance(uid, str) and self._is_local_ai(uid) and uid not in ids:
                ids.append(uid)
        return ids

    def _confirmed_room_ai_ids(self, room_id: Optional[str]) -> Tuple[Tuple[str, ...], bool, bool, Optional[str]]:
        """Membership-only local AI list for memory scoping.

        Mentions are suitable for deciding who should reply, but are not proof
        that a room is a DM.  A failed/cold member source therefore returns an
        unconfirmed result and memory uses group scope fail-closed.
        """
        if not room_id or self._room_members is None:
            return (), False, False, None
        try:
            members = tuple(dict.fromkeys(
                uid for uid in self._room_members(room_id) or () if isinstance(uid, str)
            ))
            ids = []
            for user_id in members:
                if self._is_local_ai(user_id) and user_id not in ids:
                    ids.append(user_id)
            # Q5: a DM is exactly one local persona plus one local human.  AS
            # senders, foreign users, and any extra member make it a group.
            humans = [user_id for user_id in members if self._is_local_human(user_id)]
            is_dm = len(members) == 2 and len(ids) == 1 and len(humans) == 1
            return tuple(ids), True, is_dm, humans[0] if is_dm else None
        except Exception:
            self._stats.inc("room_member_errors")
            return (), False, False, None

    def _is_local_human(self, user_id: str) -> bool:
        parsed = _split_user_id(user_id)
        if parsed is None or self._server_name is None:
            return False
        localpart, domain = parsed
        return (
            domain.lower() == self._server_name
            and not localpart.startswith(AI_LOCALPART_PREFIX)
            and localpart != self._as_sender_localpart
        )

    def _reply_targets(
        self,
        *,
        room_id: Optional[str],
        text: str,
        mentions: Iterable[str],
        sender: str,
        reply_to_sender: Optional[str],
        is_ai_sender: bool,
        ai_ids: Optional[Sequence[str]] = None,
        room_is_dm: bool = False,
    ) -> Tuple[str, ...]:
        """本条消息应回复的**全部**本服 AI，按提及顺序（PR2 逐个发言）。

        - sender 自己是 AI → 空（防循环）；
        - 已确认 DM（成员恰为该本服 AI 和一个本服人类）→ 该 AI（必回）；
        - 群聊 → 被 `m.mentions.user_ids` 点名或被正文写出完整 user ID 的本服
          AI，按 mention 顺序；此外若回复某个本服 AI 的消息（`reply_to_sender` 是
          它）→ 也回（追加在末尾，去重）。
        - 外服 `@ai_*` 永不在结果里（`_is_local_ai` fail-closed）。
        """
        if is_ai_sender:
            return ()
        ai_ids = list(ai_ids) if ai_ids is not None else self._room_ai_ids(room_id, mentions)
        others = [uid for uid in ai_ids if uid != sender]
        if not others:
            return ()
        if room_is_dm and len(others) == 1:
            return (others[0],)

        targets: list = []
        for uid in mentions:
            if uid in others and uid not in targets:
                targets.append(uid)
        # 无结构化提及时按正文首次出现顺序；其余正文提及追加在结构化列表后。
        for uid in sorted(
            (uid for uid in others if _has_full_user_id(text, uid)), key=text.index
        ):
            if uid not in targets:
                targets.append(uid)
        if reply_to_sender is not None and reply_to_sender in others:
            if reply_to_sender not in targets:
                targets.append(reply_to_sender)
        return tuple(targets)

    def _reply_target_sender(
        self, reply_event_id: Optional[str], room_id: Optional[str]
    ) -> Optional[str]:
        """解析被回复事件的 sender：进程内缓存 → 可选回填 fetcher → None。

        回填 fetcher 通常实现为 AS 传输的
        `GET /_matrix/client/v3/rooms/{roomId}/event/{eventId}`；任何异常
        （网络错误、超时、HTTP 错误、响应结构不对）都**吞掉**并返回 None，
        计 `reply_lookup_errors`，语义上等价于「不是回复」（fail-closed）——
        查不到被回复对象不能把整条事务标 failed，更不能凭空判定「被 AI 回复」。
        命中缓存 / fetcher 成功后写回缓存；日志不含 event_id / room_id。
        """
        if not reply_event_id:
            return None
        cached = self._sender_cache.get(reply_event_id)
        if cached is not None:
            self._stats.inc("reply_target_cached")
            return cached
        if self._sender_fetcher is None or not room_id:
            return None
        try:
            sender = self._sender_fetcher(room_id, reply_event_id)
        except Exception:  # noqa: BLE001 - 回填失败按「不是回复」处理
            self._stats.inc("reply_lookup_errors")
            return None
        if not isinstance(sender, str) or not sender:
            return None
        self._stats.inc("reply_target_fetched")
        self._sender_cache.remember(reply_event_id, sender)
        return sender

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


def _has_full_user_id(text: str, user_id: str) -> bool:
    """Match a complete Matrix ID, never a prefix of a longer user ID."""
    return re.search(
        r"(?<![A-Za-z0-9._=-])" + re.escape(user_id) + r"(?![A-Za-z0-9._=-])",
        text,
    ) is not None
