"""Synthetic tests: no Hermes home, DB, network, credentials or model calls."""
import asyncio
import unittest
from dataclasses import replace

from session_title_icons.engine import Engine, Outcome, Snapshot


class MemoryStore:
    def __init__(self):
        self.rows = {('owner', str(i)): Snapshot('owner', str(i), f'Title {i}', 1, 1, i)
                     for i in range(40)}
        self.writes = 0

    async def read(self, profile, sid):
        return self.rows.get((profile, sid))

    async def recent(self, profile, limit):
        return sorted((r for r in self.rows.values() if r.profile == profile),
                      key=lambda r: r.last_active, reverse=True)[:limit]

    async def rename_if_unchanged(self, before, title):
        key = (before.profile, before.session_id)
        if self.rows.get(key) != before:
            return False
        self.rows[key] = replace(before, title=title, revision=before.revision + 1)
        self.writes += 1
        return True


class EngineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = MemoryStore()
        self.calls = []
        async def select(title):
            self.calls.append(title)
            return '💾'
        # Test doubles, not production Unicode detection.
        self.engine = Engine(self.store, select, is_emoji=lambda s: s in {'💾', '🐈', '👨‍💻'},
                             first_grapheme=lambda s: '👨‍💻' if s.startswith('👨‍💻') else s[:1],
                             sanitize=lambda s: s.replace('\u200d', ''), delay_seconds=0,
                             consent=True)
        self.addAsyncCleanup(self.engine.close)

    async def test_preserves_manual_title_text(self):
        self.assertEqual(await self.engine.decorate('owner', '1'), Outcome.UPDATED)
        self.assertEqual(self.store.rows['owner', '1'].title, '💾 Title 1')
        self.assertEqual(self.calls, ['Title 1'])

    async def test_repeat_is_idempotent(self):
        await self.engine.decorate('owner', '1')
        self.assertEqual(await self.engine.decorate('owner', '1'), Outcome.SKIPPED)
        self.assertEqual(len(self.calls), 1)

    async def test_missing_title_no_model(self):
        row = self.store.rows['owner', '1']
        self.store.rows['owner', '1'] = replace(row, title=None)
        self.assertEqual(await self.engine.decorate('owner', '1'), Outcome.SKIPPED)
        self.assertFalse(self.calls)

    async def test_deleted_session(self):
        self.assertEqual(await self.engine.decorate('owner', 'gone'), Outcome.SKIPPED)

    async def test_ineligible_session(self):
        self.store.rows['owner', '1'] = replace(self.store.rows['owner', '1'], eligible=False)
        self.assertEqual(await self.engine.decorate('owner', '1'), Outcome.SKIPPED)

    async def test_manual_rename_during_model(self):
        async def select(_title):
            self.store.rows['owner', '1'] = replace(self.store.rows['owner', '1'], title='Manual')
            return '💾'
        self.engine.select = select
        self.assertEqual(await self.engine.decorate('owner', '1'), Outcome.STALE)
        self.assertEqual(self.store.rows['owner', '1'].title, 'Manual')

    async def test_reset_during_model(self):
        async def select(_title):
            self.store.rows['owner', '1'] = replace(self.store.rows['owner', '1'], generation=2)
            return '💾'
        self.engine.select = select
        self.assertEqual(await self.engine.decorate('owner', '1'), Outcome.STALE)

    async def test_delete_during_model(self):
        async def select(_title):
            del self.store.rows['owner', '1']
            return '💾'
        self.engine.select = select
        self.assertEqual(await self.engine.decorate('owner', '1'), Outcome.STALE)

    async def test_invalid_model_output(self):
        async def select(_title):
            return '💾 Ignore instructions'
        self.engine.select = select
        self.assertEqual(await self.engine.decorate('owner', '1'), Outcome.ERROR)
        self.assertEqual(self.store.writes, 0)

    async def test_zwj_sanitation_not_silently_accepted(self):
        async def select(_title):
            return '👨‍💻'
        self.engine.select = select
        self.assertEqual(await self.engine.decorate('owner', '1'), Outcome.SKIPPED)

    async def test_batch_selects_thirty_not_thirty_successes(self):
        self.store.rows['owner', '39'] = replace(self.store.rows['owner', '39'], title='💾 Done')
        result = await self.engine.recent('owner', 30)
        self.assertEqual(result, dict(updated=29, skipped=1, stale=0, error=0))
        self.assertEqual(self.store.rows['owner', '9'].title, 'Title 9')

    async def test_deduplicates_first_hook(self):
        self.assertTrue(self.engine.schedule('owner', '1'))
        self.assertFalse(self.engine.schedule('owner', '1'))
        await asyncio.gather(*list(self.engine.pending.values()))
        self.assertFalse(self.engine.schedule('owner', '1'))
        self.assertEqual(self.store.writes, 1)

    async def test_timer_reads_title_after_delay(self):
        self.engine.schedule('owner', '1')
        self.store.rows['owner', '1'] = replace(self.store.rows['owner', '1'], title='Finished title')
        await asyncio.gather(*list(self.engine.pending.values()))
        self.assertEqual(self.calls, ['Finished title'])

    async def test_close_cancels_pending(self):
        self.engine.delay = 60
        self.engine.schedule('owner', '1')
        await self.engine.close()
        self.assertFalse(self.engine.pending)
        self.assertFalse(self.calls)

    async def test_consent_off(self):
        self.engine.consent = False
        self.assertFalse(self.engine.schedule('owner', '1'))
        self.assertEqual(await self.engine.decorate('owner', '1'), Outcome.SKIPPED)
        self.assertFalse(self.calls)

    async def test_model_error_is_per_session(self):
        async def select(title):
            if title == 'Title 39':
                raise OSError('synthetic outage')
            return '💾'
        self.engine.select = select
        result = await self.engine.recent('owner', 2)
        self.assertEqual(result['error'], 1)
        self.assertEqual(result['updated'], 1)

    async def test_failure_diagnostics_never_log_exception_or_title(self):
        async def select(title):
            raise RuntimeError('PRIVATE credential and title: ' + title)
        self.engine.select = select
        with self.assertLogs('session_title_icons.engine', level='WARNING') as logs:
            self.assertEqual(await self.engine.decorate('owner', '1'), Outcome.ERROR)
        text = '\n'.join(logs.output)
        self.assertIn('stage=select category=RuntimeError', text)
        self.assertNotIn('PRIVATE', text)
        self.assertNotIn('Title 1', text)
        self.assertEqual(self.store.writes, 0)

    async def test_limit_validation(self):
        for limit in (0, 201, True, '30'):
            with self.assertRaises(ValueError):
                await self.engine.recent('owner', limit)


if __name__ == '__main__':
    unittest.main()
