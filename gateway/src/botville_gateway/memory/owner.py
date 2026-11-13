# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 botville contributors
"""Owner-memory storage adapter used by the HTTP gateway.

It has no authentication policy.  The HTTP layer resolves the caller before
using this adapter, while this module keeps public IDs and mutations atomic.
"""
from __future__ import annotations

import hashlib
import hmac
import base64
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, Optional, Tuple

from .store import MemoryStore, OWNER_PIN_LIMIT


class OwnerMemoryService:
    def __init__(self, store: MemoryStore, hmac_secret: Any) -> None:
        self.store = store
        self.store.set_hmac_secret(hmac_secret)
        self._secret = hmac_secret if isinstance(hmac_secret, bytes) else hmac_secret.encode('utf-8')

    def counterpart_hmac(self, counterpart: Optional[str]) -> Optional[str]:
        if not counterpart:
            return None
        return hmac.new(self._secret, counterpart.encode('utf-8'), hashlib.sha256).hexdigest()

    def _expiry(self, timestamp: str, pinned: bool) -> Optional[str]:
        if pinned:
            return None
        try:
            point = datetime.strptime(timestamp, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            return None
        return (point + timedelta(days=self.store.retention_days)).strftime('%Y-%m-%dT%H:%M:%SZ')

    def _memory(self, layer: str, row: Dict[str, Any]) -> Dict[str, Any]:
        pinned = bool(row.get('pinned', False))
        scope_kind = row.get('scope_kind', 'dm')
        scope: Dict[str, Any] = {'kind': scope_kind}
        if layer == 'profile':
            scope['counterpart'] = row['counterpart']
        elif scope_kind == 'group':
            scope['room_id'] = row.get('room_id') or row.get('scope_key')
        else:
            scope['counterpart'] = row.get('counterpart') or row.get('scope_key')
        if layer == 'recent':
            source: Dict[str, Any] = {'kind': 'event', 'event_id': row.get('event_id'),
                                      'sender': row.get('sender'), 'role': row.get('role')}
            updated = row.get('updated_at') or row['created_at']
        elif layer == 'summary':
            source = {'kind': 'summary', 'model': row.get('model')}
            updated = row['updated_at']
        else:
            source = {'kind': row.get('source', 'owner')}
            updated = row['updated_at']
        source = {key: value for key, value in source.items() if value is not None}
        result = {
            'id': row['public_id'], 'persona_id': row['persona_id'], 'layer': layer,
            'scope': scope, 'visibility': row.get('visibility', 'scope_only'),
            'trust': ('owner' if row.get('source') == 'owner' else 'derived')
                if layer == 'profile' else row.get('trust', 'derived'),
            'source': source, 'pinned': pinned,
            'text': row.get('text') if layer != 'profile' else None,
            'key': row.get('key') if layer == 'profile' else None,
            'value': row.get('value') if layer == 'profile' else None,
            'dirty': bool(row.get('dirty', False)) if layer == 'summary' else False,
            'redacted': False, 'created_at': row.get('created_at', updated),
            'updated_at': updated, 'expires_at': self._expiry(
                row['created_at'] if layer == 'recent' else updated, pinned),
            '_row_id': row['id'], '_counterpart': row.get('counterpart'),
        }
        if layer != 'profile':
            result.pop('key')
            result.pop('value')
        if layer != 'summary':
            result.pop('dirty')
        return result

    def _rows(self, persona_id: str) -> Iterable[Tuple[str, Dict[str, Any]]]:
        with self.store._lock:
            for table, layer in (('memory_items', 'recent'), ('memory_summaries', 'summary'),
                                 ('memory_facts', 'profile')):
                for row in self.store._db.execute('SELECT * FROM ' + table + ' WHERE persona_id=?', (persona_id,)):
                    yield layer, dict(row)

    def list(self, persona_id: str, *, layers: Iterable[str] = (), scope_kind: Optional[str] = None,
             counterpart: Optional[str] = None, room_id: Optional[str] = None,
             pinned: Optional[bool] = None, cursor: Optional[str] = None,
             limit: int = 20) -> Tuple[list[Dict[str, Any]], Optional[str]]:
        wanted = set(layers) or {'recent', 'summary', 'profile'}
        filter_tag = hashlib.sha256(json.dumps([persona_id, sorted(wanted), scope_kind, counterpart, room_id, pinned],
                                                separators=(',', ':'), ensure_ascii=True).encode('utf-8')).hexdigest()[:16]
        decoded = None
        if cursor:
            try:
                raw, signature = cursor.rsplit('.', 1)
                expected = hmac.new(self._secret, raw.encode('ascii'), hashlib.sha256).hexdigest()[:16]
                decoded_json = base64.urlsafe_b64decode(raw.encode('ascii') + b'===').decode('ascii')
                value = json.loads(decoded_json)
                if (not hmac.compare_digest(signature, expected) or not isinstance(value, list) or len(value) != 4
                        or value[0] != filter_tag or not isinstance(value[1], str)
                        or not isinstance(value[2], str) or not isinstance(value[3], str)):
                    raise ValueError()
                decoded = tuple(value[1:])
            except (ValueError, UnicodeError, json.JSONDecodeError):
                raise ValueError('invalid memory cursor')
        # Each layer has a different physical shape.  Query each one with its
        # own scope projection, then merge only the bounded candidates.
        def rows_for(table: str, layer: str):
            where, args = ['persona_id=?'], [persona_id]
            if layer == 'profile':
                if scope_kind and scope_kind != 'dm': return []
                if room_id: return []
                if counterpart: where.append('counterpart=?'); args.append(counterpart)
            else:
                if scope_kind: where.append('scope_kind=?'); args.append(scope_kind)
                if counterpart:
                    projection = 'scope_key' if layer == 'summary' else "coalesce(nullif(counterpart,''),scope_key)"
                    where.append("scope_kind='dm' AND " + projection + "=?")
                    args.append(counterpart)
                if room_id:
                    projection = 'scope_key' if layer == 'summary' else "coalesce(nullif(room_id,''),scope_key)"
                    where.append("scope_kind='group' AND " + projection + "=?")
                    args.append(room_id)
            if pinned is not None:
                if layer == 'summary':
                    if pinned:
                        return []
                else:
                    where.append('pinned=?'); args.append(int(pinned))
            timestamp = 'updated_at'
            if decoded:
                where.append("(" + timestamp + " < ? OR (" + timestamp + " = ? AND public_id < ?))")
                args.extend((decoded[0], decoded[0], decoded[2]))
            sql = ('SELECT * FROM ' + table + ' WHERE ' + ' AND '.join(where) +
                   ' ORDER BY ' + timestamp + ' DESC, public_id DESC LIMIT ?')
            args.append(limit + 1)
            with self.store._lock:
                return [self._memory(layer, dict(row)) for row in self.store._db.execute(sql, args)]

        result = []
        if 'recent' in wanted: result.extend(rows_for('memory_items', 'recent'))
        if 'summary' in wanted: result.extend(rows_for('memory_summaries', 'summary'))
        if 'profile' in wanted: result.extend(rows_for('memory_facts', 'profile'))
        result.sort(key=lambda item: (item['updated_at'], item['id']), reverse=True)
        page = result[:limit]
        next_cursor: Optional[str] = None
        if len(result) > limit and page:
            last = page[-1]
            encoded = json.dumps([filter_tag, last['updated_at'], '', last['id']], separators=(',', ':'))
            raw = base64.urlsafe_b64encode(encoded.encode('ascii')).decode('ascii').rstrip('=')
            next_cursor = raw + '.' + hmac.new(self._secret, raw.encode('ascii'), hashlib.sha256).hexdigest()[:16]
        return page, next_cursor

    def get(self, public_id: str, persona_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        prefix = {'mr_': ('memory_items', 'recent'), 'ms_': ('memory_summaries', 'summary'),
                  'mf_': ('memory_facts', 'profile')}.get(public_id[:3])
        if not prefix:
            return None
        table, layer = prefix
        query = 'SELECT * FROM ' + table + ' WHERE public_id=?'
        args: tuple[Any, ...] = (public_id,)
        if persona_id is not None:
            query += ' AND persona_id=?'
            args += (persona_id,)
        with self.store._lock:
            row = self.store._db.execute(query, args).fetchone()
        return self._memory(layer, dict(row)) if row else None

    def delete(self, persona_id: str, public_id: str) -> Optional[Dict[str, Any]]:
        with self.store._lock:
            item = self.get(public_id, persona_id)
            if not item:
                return None
            if item['layer'] == 'recent':
                removed = self.store.delete_item(persona_id, item['_row_id'])
            elif item['layer'] == 'profile':
                removed = self.store.delete_fact(persona_id, item['key'], item['_counterpart'])
            else:
                # The watermark lives on this row. Absence restarts the next
                # summary from zero; the source items are deliberately retained.
                with self.store._db:
                    removed = self.store._db.execute(
                        'DELETE FROM memory_summaries WHERE id=? AND persona_id=?',
                        (item['_row_id'], persona_id)).rowcount
            return item if removed else None

    def pin(self, persona_id: str, public_id: str, pinned: bool) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        with self.store._lock, self.store._db:
            self.store._db.execute('BEGIN IMMEDIATE')
            item = self.get(public_id, persona_id)
            if not item:
                return None, 'not_found'
            if item['layer'] == 'summary':
                return item, 'not_pinnable'
            if item['pinned'] == pinned:
                return item, None
            if pinned:
                count = self.store._db.execute(
                    'SELECT (SELECT count(*) FROM memory_items WHERE persona_id=? AND pinned=1)'
                    ' + (SELECT count(*) FROM memory_facts WHERE persona_id=? AND pinned=1)',
                    (persona_id, persona_id)).fetchone()[0]
                if count >= OWNER_PIN_LIMIT:
                    return item, 'pin_limit'
            table = 'memory_items' if item['layer'] == 'recent' else 'memory_facts'
            self.store._db.execute('UPDATE ' + table + ' SET pinned=?,updated_at=? WHERE id=?',
                (int(pinned), datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'), item['_row_id']))
            return self.get(public_id, persona_id), None

    def set_fact(self, persona_id: str, counterpart: str, key: str, value: str,
                 visibility: str = 'dm_only') -> Tuple[Dict[str, Any], bool]:
        with self.store._lock:
            before = self.store._db.execute(
                'SELECT 1 FROM memory_facts WHERE persona_id=? AND counterpart=? AND key=?',
                (persona_id, counterpart, key)).fetchone()
            self.store.upsert_fact(persona_id, counterpart, key, value, visibility, 'owner', [], 'owner')
            row = self.store._db.execute(
                'SELECT public_id FROM memory_facts WHERE persona_id=? AND counterpart=? AND key=?',
                (persona_id, counterpart, key)).fetchone()
            return self.get(row['public_id'], persona_id), not bool(before)

    def audit(self, *, caller: str, request_id: Optional[str], action: str,
              persona_id: Optional[str], memory_id: Optional[str], layer: Optional[str],
              counterpart: Optional[str], result_code: str) -> None:
        # Invalid pre-persona requests still have an audit row without attacker
        # supplied identifiers.  Counterparts are HMACed before reaching SQLite.
        self.store.audit(request_id=request_id, caller=caller, persona_id=persona_id or '',
                         action=action, memory_id=memory_id, layer=layer,
                         result_code=result_code, counterpart_hmac=self.counterpart_hmac(counterpart))
