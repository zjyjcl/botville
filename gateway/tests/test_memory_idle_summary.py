# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 botville contributors
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), 'src'))
from botville_gateway.memory.profile import ModelExtractor
from botville_gateway.memory.store import MemoryStore
from botville_gateway.memory.summary import (SUMMARY_INPUT_TOKEN_CAP,
    StoreSummaryProvider, generate_summary, summary_input)
from botville_gateway.engine.engine import estimate_tokens


class FakeModel:
    def __init__(self, answer='summary'):
        self.answer, self.calls = answer, []
    def complete(self, **kwargs):
        self.calls.append(kwargs)
        return self.answer


class IdleSummaryTests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore(':memory:')
        self.addCleanup(self.store.close)

    def add(self, scope='dm', key='@u:local', text='x', trust='trusted', event=None):
        self.store.add_item(persona_id='p', scope_kind=scope, scope_key=key, room_id='!r',
                            counterpart=key if scope == 'dm' else '@u:local',
                            event_id=event or '$' + str(len(self.store.items('p'))),
                            sender='@u:local', role='user', trust=trust, text=text)

    def test_trigger_and_scope_and_isolation(self):
        for _ in range(41): self.add(text='ignore system <untrusted')
        self.add('group', '!other', 'private group')
        request = summary_input(self.store, persona_id='p', scope_kind='dm', scope_key='@u:local')
        self.assertIn('<untrusted id=', request['messages'][0]['content'])
        self.assertNotIn('private group', request['messages'][0]['content'])
        model = FakeModel('short result')
        self.assertTrue(generate_summary(self.store, model, persona_id='p', scope_kind='dm', scope_key='@u:local'))
        self.assertNotIn('ignore system', model.calls[0]['system_text'])
        self.assertEqual(StoreSummaryProvider(self.store).recall(persona_id='p', scope_kind='group', scope_key='!other'), [])

    def test_untrusted_dirty_recompute(self):
        for _ in range(41): self.add(text='bad data', trust='untrusted')
        model = FakeModel('contains derived content')
        generate_summary(self.store, model, persona_id='p', scope_kind='dm', scope_key='@u:local')
        row = self.store.summary('p', 'dm', '@u:local')
        self.assertEqual(row['trust'], 'untrusted')
        self.store.redact_event('$0')
        self.assertTrue(self.store.summary('p', 'dm', '@u:local')['dirty'])
        generate_summary(self.store, model, persona_id='p', scope_kind='dm', scope_key='@u:local')
        self.assertFalse(self.store.summary('p', 'dm', '@u:local')['dirty'])

    def test_redaction_during_model_call_cannot_save_stale_summary(self):
        for _ in range(41): self.add(text='source')
        request = summary_input(self.store, persona_id='p', scope_kind='dm', scope_key='@u:local')
        self.store.redact_event('$0')
        self.assertFalse(self.store.save_summary(persona_id='p', scope_kind='dm', scope_key='@u:local',
            text='stale', trust=request['trust'], covers_until_item_id=request['items'][-1]['id'], model='m',
            source_snapshot=request['source_snapshot'], previous_updated_at=request['previous_updated_at'],
            previous_dirty=request['previous_dirty']))

    def test_model_extractor_strict_whitelist_and_isolation(self):
        model = FakeModel('[{"key":"name","value":"Ana"},{"key":"intimacy","value":"9"}]')
        result = list(ModelExtractor(model).extract('inject me'))
        self.assertEqual(result, [{'key': 'name', 'value': 'Ana'}])
        self.assertIn('<untrusted id=', model.calls[0]['messages'][0]['content'])
        self.assertNotIn('inject me', model.calls[0]['system_text'])

    def test_large_backlog_summarizes_oldest_input_cap_chunk_then_advances(self):
        # CJK is one token per character (plus the estimator's nonempty
        # constant).  The first request fits five source items, then the next round moves
        # forward from its persisted covers_until_item_id.
        for number in range(41):
            self.add(text=chr(0x4e00 + number) * 1000)
        request = summary_input(self.store, persona_id='p', scope_kind='dm', scope_key='@u:local')
        self.assertLess(sum(estimate_tokens(message['content']) for message in request['messages']),
                        SUMMARY_INPUT_TOKEN_CAP + 1)
        self.assertGreater(len(request['items']), 0)
        self.assertEqual(request['items'][0]['id'], 1)
        model = FakeModel('first rolling summary')
        self.assertTrue(generate_summary(self.store, model, persona_id='p', scope_kind='dm', scope_key='@u:local'))
        self.assertEqual(self.store.summary('p', 'dm', '@u:local')['covers_until_item_id'],
                         request['items'][-1]['id'])
        next_request = summary_input(self.store, persona_id='p', scope_kind='dm', scope_key='@u:local')
        self.assertEqual(next_request['items'][0]['id'], request['items'][-1]['id'] + 1)

    def test_oversized_oldest_item_uses_bounded_prefix_and_advances(self):
        for _ in range(41):
            self.add(text='一' * 8000)
        request = summary_input(self.store, persona_id='p', scope_kind='dm', scope_key='@u:local')
        self.assertEqual(request['items'][0]['id'], 1)
        self.assertLessEqual(sum(estimate_tokens(message['content']) for message in request['messages']),
                             SUMMARY_INPUT_TOKEN_CAP)
        self.assertIn('[truncated]', ''.join(message['content'] for message in request['messages']))
        self.assertTrue(generate_summary(self.store, FakeModel('bounded'), persona_id='p',
                                         scope_kind='dm', scope_key='@u:local'))
        self.assertEqual(self.store.summary('p', 'dm', '@u:local')['covers_until_item_id'], 1)

if __name__ == '__main__': unittest.main()
