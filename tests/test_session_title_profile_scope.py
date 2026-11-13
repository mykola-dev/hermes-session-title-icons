"""Native secret-scope regression, using synthetic credentials and no network."""
import asyncio
import contextlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from session_title_icons.queue import Queue
from session_title_icons.runtime import Resident, model_profile_scope


class ProfileScopeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        (self.home / '.env').write_text('HERMES_CODEX_BASE_URL=https://profile.invalid\n')
        (self.home / 'config.yaml').write_text('{}\n')
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch('agent.secret_scope._MULTIPLEX_ACTIVE', True))
        self.stack.enter_context(patch('hermes_constants.get_process_hermes_home', return_value=self.home))
        self.stack.enter_context(patch('tui_gateway.launch_profile_policy._launch_env',
                                      return_value={'HERMES_CODEX_BASE_URL': 'https://launch.invalid'}))
        from agent.secret_scope import set_secret_scope, reset_secret_scope
        token = set_secret_scope(None)
        self.addCleanup(reset_secret_scope, token)

    async def test_unscoped_background_reproduces_live_failure(self):
        from agent.secret_scope import get_secret, UnscopedSecretError
        with self.assertRaises(UnscopedSecretError):
            get_secret('HERMES_CODEX_BASE_URL')
        with model_profile_scope(self.home):
            self.assertEqual(get_secret('HERMES_CODEX_BASE_URL'), 'https://profile.invalid')
        with self.assertRaises(UnscopedSecretError):
            get_secret('HERMES_CODEX_BASE_URL')

    async def test_scope_rebuilds_per_job_and_restores_after_exception(self):
        from agent.secret_scope import get_secret, current_secret_scope
        with self.assertRaisesRegex(ValueError, 'synthetic'):
            with model_profile_scope(self.home):
                raise ValueError('synthetic')
        self.assertIsNone(current_secret_scope())
        (self.home / '.env').write_text('HERMES_CODEX_BASE_URL=https://rotated.invalid\n')
        with model_profile_scope(self.home):
            self.assertEqual(get_secret('HERMES_CODEX_BASE_URL'), 'https://rotated.invalid')
        self.assertIsNone(current_secret_scope())

    async def test_foreign_home_is_refused(self):
        with self.assertRaises(PermissionError):
            with model_profile_scope(self.home / 'foreign'):
                self.fail('Foreign profile must never borrow launch credentials')

    async def test_resident_scopes_auto_and_batch_without_caller_identity(self):
        from agent.secret_scope import get_secret, current_secret_scope
        from session_title_icons.engine import Outcome
        queue = Queue(self.home / 'queue')
        seen = []

        class Engine:
            async def decorate(_self, profile, sid):
                seen.append(get_secret('HERMES_CODEX_BASE_URL'))
                return Outcome.UPDATED

            async def recent(_self, profile, limit):
                seen.append(get_secret('HERMES_CODEX_BASE_URL'))
                return {'updated': 1}

        engine = Engine()
        resident = Resident(queue, lambda: engine, 'default', lambda: True,
                            scope_factory=lambda: model_profile_scope(self.home))
        queue.schedule('synthetic', 0)
        await resident._process(engine, queue.claim())
        queue.batch('profile:default', 1)
        await resident._process(engine, queue.claim())
        self.assertEqual(seen, ['https://profile.invalid'] * 2)
        self.assertIsNone(current_secret_scope())

    async def test_cancellation_restores_scope(self):
        from agent.secret_scope import current_secret_scope
        with self.assertRaises(asyncio.CancelledError):
            with model_profile_scope(self.home):
                raise asyncio.CancelledError
        self.assertIsNone(current_secret_scope())
