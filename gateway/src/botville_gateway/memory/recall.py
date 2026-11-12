# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 botville contributors
"""Scope-filtered recall; every memory stays in an isolated data segment."""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Protocol, Sequence

from ..isolation import build_untrusted_segment, generate_nonce
from .store import MemoryStore

RECALL_TOKEN_BUDGET = 2000
RECALL_ITEMS_LIMIT = 200
RECALL_FACTS_LIMIT = 100
RECALL_PEOPLE_LIMIT = 100


class SummaryProvider(Protocol):
    """PR3b adapter: return only summaries for the requested persona and scope.

    Rows carry text, created_at, trust, source and visibility. Any explicit
    persona/scope metadata is checked again by the assembler. Outputs remain
    untrusted regardless of their input sources.
    """

    def recall(self, *, persona_id: str, scope_kind: str,
               scope_key: str) -> List[Dict[str, Any]]: ...


class VectorRecall(Protocol):
    """PR3b adapter with the same scope/source contract as SummaryProvider."""

    def recall(self, *, persona_id: str, scope_kind: str,
               scope_key: str) -> List[Dict[str, Any]]: ...


def assemble_recall(
    store: MemoryStore, *, persona_id: str, scope_kind: str, scope_key: str,
    counterpart: str = '', mentioned_counterparts: Sequence[str] = (),
    budget: int = RECALL_TOKEN_BUDGET,
    summary_provider: Optional[SummaryProvider] = None,
    vector_recall: Optional[VectorRecall] = None,
) -> List[Dict[str, str]]:
    """Fit isolated segments, including metadata and wrappers, to the budget.

    Priority is pinned, profile, summary, vector, recent; within each tier newest
    comes first. The final fitting item is truncated, never displaced by a lower
    tier. A budget too small for a complete wrapper yields no segment. The current
    live human turn is supplied separately by the engine, outside this budget.
    """
    # Lazy import avoids engine -> memory -> engine initialization cycles.
    from ..engine.engine import estimate_tokens

    if scope_kind not in ('dm', 'group') or not scope_key:
        raise ValueError('invalid memory recall scope')
    if not isinstance(budget, int) or isinstance(budget, bool) or budget < 0:
        raise ValueError('invalid memory recall budget')
    if scope_kind == 'dm' and counterpart != scope_key:
        # DM facts must belong to the same counterpart as the raw dialogue.
        raise ValueError('memory counterpart does not match DM scope')

    candidates = []
    people = ((counterpart,) if scope_kind == 'dm' else
              tuple(dict.fromkeys((counterpart, *mentioned_counterparts))))
    for person in people[:RECALL_PEOPLE_LIMIT]:
        if not person:
            continue
        for fact in store.facts(persona_id, person, limit=RECALL_FACTS_LIMIT,
                                visibility='shared' if scope_kind == 'group' else None):
            if scope_kind == 'group' and fact['visibility'] != 'shared':
                continue
            candidates.append((0 if fact['pinned'] else 1, 'profile',
                               fact['updated_at'], fact['trust'],
                               f"{fact['key']}: {fact['value']}"))
    recent_entries = {}
    for item in store.items(persona_id, scope_kind=scope_kind, scope_key=scope_key,
                            limit=RECALL_ITEMS_LIMIT):
        entry = (0 if item['pinned'] else 4, 'recent',
                 item['created_at'], item['trust'], item['text'])
        recent_entries[item['id']] = entry
        candidates.append(entry)
    summary_ids = set()
    for provider, rank, layer in ((summary_provider, 2, 'summary'),
                                  (vector_recall, 3, 'vector')):
        if provider is None:
            continue
        try:
            rows = provider.recall(persona_id=persona_id, scope_kind=scope_kind,
                                   scope_key=scope_key)
        except Exception:
            if layer == 'vector':
                continue
            raise
        for row in rows:
            if layer == 'summary' and row.get('id') is not None:
                summary_ids.add(row['id'])
            if any(row.get(key, value) != value for key, value in (
                    ('persona_id', persona_id), ('scope_kind', scope_kind),
                    ('scope_key', scope_key))):
                continue
            if scope_kind == 'group' and row.get('visibility') == 'dm_only':
                continue
            if layer == 'vector':
                if row.get('summary_id') in summary_ids:
                    continue
                recent = recent_entries.pop(row.get('item_id'), None)
                if recent is not None:
                    if recent[0] == 0:  # Preserve the higher pinned priority.
                        continue
                    candidates.remove(recent)  # Promote once to vector priority.
            if isinstance(row.get('text'), str):
                candidates.append((0 if row.get('pinned') else rank, layer,
                                   str(row.get('created_at', '')), 'untrusted', row['text']))

    # Stable sorts retain store's descending id order for same-second entries.
    candidates.sort(key=lambda entry: entry[2], reverse=True)
    candidates.sort(key=lambda entry: entry[0])
    output = []
    remaining = budget
    for _, layer, stamp, trust, text in candidates:
        prefix = f'[memory layer={layer} time={stamp} trust={trust}]\n'
        text = text[:max(0, 8000 - len(prefix))]
        nonce = generate_nonce()

        def segment(length: int) -> str:
            return build_untrusted_segment(prefix + text[:length], nonce=nonce)[0]

        wrapped = segment(len(text))
        if estimate_tokens(wrapped) > remaining:
            # Count actual normalized/escaped output, not raw text. A fixed nonce
            # keeps length stable during the search and preserves closed tags.
            if estimate_tokens(segment(0)) > remaining:
                break
            low, high = 0, len(text)
            while low < high:
                mid = (low + high + 1) // 2
                if estimate_tokens(segment(mid)) <= remaining:
                    low = mid
                else:
                    high = mid - 1
            wrapped = segment(low)
            output.append({'role': 'user', 'content': wrapped})
            break
        output.append({'role': 'user', 'content': wrapped})
        remaining -= estimate_tokens(wrapped)
    return output
