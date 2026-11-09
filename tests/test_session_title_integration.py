"""Real pinned storage/facade/loader, synthetic models and mutation transports."""
import asyncio
import contextlib
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from session_title_icons.engine import Engine, Outcome
from session_title_icons.queue import Queue
from session_title_icons.runtime import Resident
from session_title_icons.selector import Selector
from session_title_icons.store import HermesStore
from session_title_icons.tools import Tools
from session_title_icons.unicode_emoji import Emoji


class UnicodeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.emoji = Emoji()

    def test_standard_sequences(self):
        for value in ('💾', '🐈', '🇺🇦', '1️⃣', '👨‍💻', '👍🏽', '❤️', '☀'):
            self.assertTrue(self.emoji.is_emoji(value), value)
            self.assertEqual(self.emoji.first(value + ' Title'), value)

    def test_non_emoji_and_components(self):
        for value in ('', 'A', '1', '*', '#', '🏽', '🇺', '\ufe0f', '\u200d', '💾🐈', '💾 extra'):
            self.assertFalse(self.emoji.is_emoji(value), value)


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name) / 'queue'
        self.queue = Queue(self.directory)

    def test_one_first_hook_survives_reopening(self):
        self.assertTrue(self.queue.schedule('s1', 60, now=100))
        self.assertFalse(Queue(self.directory).schedule('s1', 60, now=120))
        self.assertIsNone(self.queue.claim(now=159))
        job = self.queue.claim(now=160)
        self.assertEqual(job['session_id'], 's1')
        self.assertIsNone(self.queue.claim(now=161))

    def test_concurrent_hook_inserts_deduplicate(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.queue.schedule('one', 60), range(20)))
        self.assertEqual(sum(results), 1)

    def test_claim_is_exclusive(self):
        self.queue.schedule('one', 0)
        with ThreadPoolExecutor(max_workers=8) as pool:
            claims = list(pool.map(lambda _: self.queue.claim(), range(8)))
        self.assertEqual(sum(job is not None for job in claims), 1)

    def test_batch_counts_and_status_scope(self):
        job_id = self.queue.batch('caller', 30)
        self.assertIsNone(self.queue.status(job_id, 'other'))
        job = self.queue.claim()
        self.assertEqual(job['limit_n'], 30)
        self.queue.finish(job_id, {'updated': 25, 'skipped': 5})
        self.assertEqual(self.queue.status(job_id, 'caller'), {
            'state': 'done', 'result': {'updated': 25, 'skipped': 5}})

    def test_interrupted_jobs_never_replayed(self):
        self.queue.schedule('one', 0)
        self.queue.claim()
        Queue(self.directory).recover()
        self.assertIsNone(self.queue.claim())
        self.assertFalse(self.queue.schedule('one', 0))

    def test_capacity_fails_closed(self):
        queue = Queue(self.directory, capacity=1)
        queue.schedule('one', 0)
        with self.assertRaises(RuntimeError):
            queue.schedule('two', 0)
        self.assertFalse(queue.schedule('one', 0))

    def test_invalid_batches(self):
        for limit in (True, 0, 201, '30'):
            with self.assertRaises(ValueError):
                self.queue.batch('caller', limit)


class StoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from hermes_state import SessionDB
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.db = SessionDB(self.home / 'state.db')
        self.addCleanup(self.db.close)
        self.db.create_session('synthetic', 'cli')
        self.db.set_session_title('synthetic', 'Backups')
        self.rpc = AsyncMock(return_value={'title': '💾 Backups'})
        self.store = HermesStore(self.home, 'default', rpc=self.rpc, allowed=lambda: True)

    async def test_actual_read_only_metadata_snapshot(self):
        snap = await self.store.read('default', 'synthetic')
        self.assertEqual(snap.title, 'Backups')
        self.assertTrue(snap.eligible)
        self.assertEqual(snap.revision, 0)

    async def test_actual_rich_listing(self):
        # Real picker suppresses empty/ghost sessions, so append one synthetic
        # message. The plugin itself never reads this body.
        self.db.append_message('synthetic', 'user', 'NOT SENT TO MODEL')
        rows = await self.store.recent('default', 30)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].session_id, 'synthetic')

    async def test_rpc_fences_and_payload(self):
        snap = await self.store.read('default', 'synthetic')
        self.assertTrue(await self.store.rename_if_unchanged(snap, '💾 Backups'))
        name, = self.rpc.call_args.args
        self.assertEqual(name, 'session.mutate')
        args = self.rpc.call_args.kwargs
        self.assertEqual(args['payload'], {'title': '💾 Backups'})
        self.assertEqual(args['expected_revision'], snap.revision)
        self.assertEqual(args['expected_generation'], snap.generation)
        self.assertEqual(self.db.get_session_title('synthetic'), 'Backups')

    async def test_manual_rename_before_reread_prevents_write(self):
        snap = await self.store.read('default', 'synthetic')
        self.db.set_session_title('synthetic', 'Manual')
        self.assertFalse(await self.store.rename_if_unchanged(snap, '💾 Backups'))
        self.rpc.assert_not_called()

    async def test_disable_before_write(self):
        snap = await self.store.read('default', 'synthetic')
        self.store.allowed = lambda: False
        self.assertFalse(await self.store.rename_if_unchanged(snap, '💾 Backups'))
        self.rpc.assert_not_called()

    async def test_cross_profile_denied(self):
        with self.assertRaises(PermissionError):
            await self.store.read('foreign', 'synthetic')
        with self.assertRaises(PermissionError):
            await self.store.recent('foreign', 30)

    async def test_transport_failure_not_retried(self):
        snap = await self.store.read('default', 'synthetic')
        self.rpc.side_effect = OSError('synthetic transport loss')
        with self.assertRaises(OSError):
            await self.store.rename_if_unchanged(snap, '💾 Backups')
        self.rpc.assert_awaited_once()

    async def test_source_filter(self):
        self.db.create_session('cron-test', 'cron')
        self.db.set_session_title('cron-test', 'Internal')
        self.assertFalse((await self.store.read('default', 'cron-test')).eligible)

    async def test_real_native_receipt_transaction(self):
        from hermes_state_runtime import begin_runtime_epoch, mutate_runtime_session
        epoch = begin_runtime_epoch(self.db, instance_id='isolated-test')
        async def rpc(method, **params):
            self.assertEqual(method, 'session.mutate')
            # This is the actual storage receipt transaction, NOT gateway
            # authentication/projection. Those boundaries are separate tests.
            return mutate_runtime_session(self.db, epoch=epoch, principal_id='test-owner', **params)
        self.store.rpc = rpc
        before = await self.store.read('default', 'synthetic')
        self.assertTrue(await self.store.rename_if_unchanged(before, '💾 Backups'))
        self.assertEqual(self.db.get_session_title('synthetic'), '💾 Backups')
        self.assertEqual(self.db.get_session_title_source('synthetic'), 'user')

    async def test_engine_store_selector_real_facade_input(self):
        llm = SimpleNamespace(acomplete_structured=AsyncMock(return_value=SimpleNamespace(parsed={'emoji': '💾'})))
        emoji = Emoji()
        engine = Engine(self.store, Selector(llm, consent=True, allowed=lambda: True),
                        is_emoji=emoji.is_emoji, first_grapheme=emoji.first,
                        sanitize=type(self.db).sanitize_title, consent=True)
        self.addAsyncCleanup(engine.close)
        self.assertEqual(await engine.decorate('default', 'synthetic'), Outcome.UPDATED)
        args = llm.acomplete_structured.call_args.kwargs
        self.assertEqual(json.loads(args['input'][0].text), {'title': 'Backups'})
        self.assertNotIn('enum', args['json_schema']['properties']['emoji'])

    async def test_batch_fifty_real_picker_and_native_receipts(self):
        from hermes_state_runtime import begin_runtime_epoch, mutate_runtime_session
        for i in range(50):
            sid = f'batch-{i}'
            self.db.create_session(sid, 'cli')
            self.db.append_message(sid, 'user', 'PRIVATE BODY NOT FOR SELECTOR')
            self.db.set_session_title(sid, f'Manual title {i}')
        for source in ('cron', 'kanban', 'oneshot', 'tool', 'api_server'):
            self.db.create_session('service-' + source, source)
            self.db.append_message('service-' + source, 'user', 'service')
            self.db.set_session_title('service-' + source, 'Internal ' + source)
        epoch = begin_runtime_epoch(self.db, instance_id='batch-fifty')
        async def rpc(method, **params):
            return mutate_runtime_session(self.db, epoch=epoch, principal_id='test-owner', **params)
        self.store.rpc = rpc
        llm = SimpleNamespace(acomplete_structured=AsyncMock(return_value=SimpleNamespace(parsed={'emoji': '💾'})))
        emoji = Emoji()
        engine = Engine(self.store, Selector(llm, consent=True, allowed=lambda: True),
                        is_emoji=emoji.is_emoji, first_grapheme=emoji.first,
                        sanitize=type(self.db).sanitize_title, consent=True)
        self.addAsyncCleanup(engine.close)
        result = await engine.recent('default', 50)
        self.assertEqual(result['updated'], 50)
        self.assertEqual(llm.acomplete_structured.await_count, 50)
        for i in range(50):
            self.assertEqual(self.db.get_session_title(f'batch-{i}'), f'💾 Manual title {i}')
        for call in llm.acomplete_structured.call_args_list:
            self.assertEqual(set(json.loads(call.kwargs['input'][0].text)), {'title'})
            self.assertNotIn('PRIVATE BODY', str(call))
        self.assertEqual((await engine.recent('default', 3))['updated'], 0)
        self.assertEqual(llm.acomplete_structured.await_count, 50)

    def authority(self):
        from gateway.config import Platform
        from gateway.session_authority import SessionAuthority, LiveSession
        from gateway.session_contract import Principal
        from hermes_state_runtime import begin_runtime_epoch
        epoch = begin_runtime_epoch(self.db, instance_id='isolated-authority')
        authority = SessionAuthority(SimpleNamespace(_draining=False), profile_id=str(self.home),
                                     instance_id='isolated-authority', db=self.db, epoch=epoch)
        authority.sessions['synthetic'] = LiveSession(
            SimpleNamespace(platform=Platform.LOCAL, user_id='test-owner'), 'synthetic-route')
        actor = Principal('test-owner', str(self.home), frozenset({'session:read', 'session:control'}), 'test')
        return authority, actor

    async def test_real_authority_auth_commit_and_native_publication(self):
        from gateway.session_contract import SessionRef
        from gateway.session_mutations import mutate_session
        authority, actor = self.authority()
        async def rpc(method, **params):
            return await mutate_session(authority, actor, SessionRef(str(self.home), params['session_id']), params)
        self.store.rpc = rpc
        before = await self.store.read('default', 'synthetic')
        self.assertTrue(await self.store.rename_if_unchanged(before, '💾 Backups'))
        stream = authority.sessions['synthetic'].event_stream
        events = stream.since(stream.epoch, 0)
        self.assertIn('session.updated', str(events))
        self.assertNotIn('session.title', str(events))
        self.assertEqual(self.db.get_session_title('synthetic'), '💾 Backups')

    async def test_authority_rejects_foreign_local_owner(self):
        from gateway.session_contract import SessionRef
        from gateway.session_mutations import mutate_session
        from hermes_state_runtime import RuntimeStoreError
        authority, actor = self.authority()
        actor = replace(actor, subject='foreign-owner')
        async def rpc(method, **params):
            return await mutate_session(authority, actor, SessionRef(str(self.home), params['session_id']), params)
        self.store.rpc = rpc
        before = await self.store.read('default', 'synthetic')
        with self.assertRaises(RuntimeStoreError):
            await self.store.rename_if_unchanged(before, '💾 Backups')
        self.assertEqual(self.db.get_session_title('synthetic'), 'Backups')

    async def test_accepted_residual_race_is_not_mislabeled_atomic(self):
        from hermes_state_runtime import begin_runtime_epoch, mutate_runtime_session
        epoch = begin_runtime_epoch(self.db, instance_id='accepted-race-test')
        async def rpc(method, **params):
            # After the plugin's final reread, before native receipt commit:
            self.db.set_session_title('synthetic', 'Concurrent manual title')
            return mutate_runtime_session(self.db, epoch=epoch, principal_id='owner', **params)
        self.store.rpc = rpc
        before = await self.store.read('default', 'synthetic')
        self.assertTrue(await self.store.rename_if_unchanged(before, '💾 Backups'))
        self.assertEqual(self.db.get_session_title('synthetic'), '💾 Backups')

    async def test_desktop_basic_bootstrap_through_native_authority_commit(self):
        from gateway.session_contract import Principal, SessionRef
        from gateway.session_local_migration import bind_native_transport, unbind_native_transport
        from gateway.session_mutations import mutate_session
        authority, _ = self.authority()
        self.db.create_session('desktop-history', 'desktop', user_id='basic:synthetic-owner')
        self.db.set_session_title('desktop-history', 'Manual desktop title')
        self.store.rpc = self.store._rpc
        discovery = SimpleNamespace(state='ready', endpoint=SimpleNamespace(profile_id=str(self.home)))
        subjects = []

        @contextlib.asynccontextmanager
        async def connection(*, endpoint, subject):
            # Unlike the previous generic transport doubles, enforce the real
            # bootstrap subject gate before any mutation is possible.
            OwnerIdentityTests().bootstrap(subject)
            subjects.append(subject)
            actor = Principal(subject, str(self.home), frozenset({
                'session:read', 'session:create', 'session:control'}), 'synthetic-native')
            bind_native_transport(authority, actor, {'provider': 'local',
                'profile_id': str(self.home), 'instance_id': 'isolated-authority'})
            async def rpc(method, **params):
                self.assertEqual(method, 'session.mutate')
                return await mutate_session(authority, actor,
                    SessionRef(str(self.home), params['session_id']), params)
            try:
                yield SimpleNamespace(rpc=rpc)
            finally:
                unbind_native_transport(authority, actor)

        with patch('hermes_cli.gateway_runtime.discover_gateway_endpoint', return_value=discovery), \
                patch('hermes_cli.gateway_client.connect_gateway', connection):
            before = await self.store.read('default', 'desktop-history')
            self.assertTrue(await self.store.rename_if_unchanged(before, '💾 Manual desktop title'))
        self.assertEqual(len(subjects), 1)
        self.assertNotEqual(subjects[0], 'basic:synthetic-owner')
        self.assertEqual(self.db.get_session_title('desktop-history'), '💾 Manual desktop title')


class OwnerIdentityTests(unittest.TestCase):
    def subject(self, source, user_id):
        class MetadataDB:
            def get_session(self, sid):
                return {'source': source, 'user_id': user_id}
            def close(self):
                pass
        return HermesStore('/synthetic', 'default', db_factory=MetadataDB)._owner_subject('test')

    def bootstrap(self, subject):
        from gateway.control_socket import GatewayControlServer
        from gateway.runtime_bootstrap import TicketStore
        store = TicketStore('synthetic-instance', frozenset({'/synthetic'}))
        request = {'protocol': 1, 'id': 1, 'verb': 'session-ticket', 'params': {
            'profile_id': '/synthetic', 'instance_id': 'synthetic-instance',
            'purpose': 'interactive', 'subject': subject}}
        # Actual private bootstrap subject check (not a fake accept-all transport).
        return GatewayControlServer._session_ticket(
            SimpleNamespace(ticket_store=store), request, 'uid:10000')

    def test_old_desktop_label_is_rejected_by_real_bootstrap(self):
        with self.assertRaises(PermissionError):
            self.bootstrap('basic:synthetic-owner')

    def test_desktop_basic_label_roundtrips_and_bootstraps(self):
        from hermes_state_mutation_binding import _login_label
        for source in ('desktop', 'gui'):
            subject = self.subject(source, 'basic:synthetic-owner')
            self.assertEqual(_login_label(subject), 'basic:synthetic-owner')
            self.assertTrue(self.bootstrap(subject)['ticket'])

    def test_unicode_and_colon_in_basic_username_preserved(self):
        from hermes_state_mutation_binding import _login_label
        label = 'basic:тест:owner'
        self.assertEqual(_login_label(self.subject('desktop', label)), label)

    def test_structured_login_is_not_reinterpreted(self):
        from gateway.session_identity import authenticated_subject
        subject = authenticated_subject({'provider': 'basic', 'issuer': '', 'user_id': 'test'})
        self.assertEqual(self.subject('desktop', subject), subject)
        self.assertTrue(self.bootstrap(subject)['ticket'])

    def test_native_peer_and_legacy_account_never_forwarded(self):
        for source, value in [('cli', 'uid:10000'), ('local', 'sid:S-1-test'),
                              ('desktop', 'nous:synthetic-account'), ('desktop', None)]:
            self.assertIsNone(self.subject(source, value))

    def test_unknown_or_malformed_identity_fails_closed(self):
        for value in ('admin', 'other:user', 'basic:', 'basic: user',
                      'auth:v1:invalid', 'basic:' + 'x' * 256, 123):
            with self.assertRaises(PermissionError, msg=repr(value)):
                self.subject('desktop', value)

    def test_nonlocal_source_does_not_impersonate_stored_user(self):
        self.assertIsNone(self.subject('telegram', 'basic:synthetic-owner'))


class ToolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.queue = Queue(Path(self.tmp.name) / 'queue')
        # Binding/queue compatibility tests, not human confirmation tests.
        self.tools = Tools(self.queue, 'default', lambda: True,
                           confirmer=AsyncMock(return_value=True))

    def bind(self, **overrides):
        from gateway.session_context import set_session_vars, clear_session_vars
        values = dict(platform='local', session_id='caller', profile='', cron_session='')
        values.update(overrides)
        tokens = set_session_vars(**values)
        self.addCleanup(clear_session_vars, tokens)

    async def test_batch_from_real_task_local_binding(self):
        self.bind()
        result = json.loads(await self.tools.handle({'action': 'recent', 'limit': 30}))
        self.assertTrue(result['ok'])
        self.assertEqual(result['state'], 'queued')
        status = json.loads(await self.tools.handle({'action': 'status', 'job_id': result['job_id']}))
        self.assertEqual(status['state'], 'pending')

    async def test_batch_from_actual_desktop_source_only_binder(self):
        from tui_gateway import server
        from gateway.session_context import _VAR_MAP
        session = {'source': 'desktop', 'profile_home': '/opt/data',
                   'agent': SimpleNamespace(session_id='caller'), 'cwd': '/tmp'}
        with patch.object(server, '_session_for_key', return_value=session), \
                patch.object(server, '_current_profile_name', return_value='default'):
            tokens = server._set_session_context('synthetic-ui-tab', cwd='/tmp')
        self.addCleanup(server._clear_session_context, tokens)
        self.assertEqual(_VAR_MAP['HERMES_SESSION_PLATFORM'].get(), '')
        self.assertEqual(_VAR_MAP['HERMES_SESSION_SOURCE'].get(), 'desktop')
        result = json.loads(await self.tools.handle({'action': 'recent', 'limit': 50}))
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['state'], 'queued')
        self.assertEqual(self.queue.claim()['limit_n'], 50)
        status = json.loads(await self.tools.handle({'action': 'status', 'job_id': result['job_id']}))
        self.assertEqual(status['state'], 'running')

    async def test_explicit_source_only_local_surfaces_allowed(self):
        from gateway.session_context import set_session_vars, clear_session_vars
        for source in ('local', 'cli', 'tui', 'desktop', 'gui'):
            tokens = set_session_vars(platform='', source=source, session_id='caller',
                                      profile='', cron_session='')
            try:
                self.assertEqual(self.tools.caller(), 'profile:default', source)
            finally:
                clear_session_vars(tokens)

    async def test_source_fallback_requires_explicit_bound_empty_platform_and_noncron(self):
        from gateway.session_context import _VAR_MAP, _UNSET
        self.bind(source='desktop', cron_session='')
        for platform, cron in (('', _UNSET), ('', '1'), ('cron', '')):
            _VAR_MAP['HERMES_SESSION_PLATFORM'].set(platform)
            _VAR_MAP['HERMES_CRON_SESSION'].set(cron)
            self.assertIsNone(self.tools.caller())

    async def test_service_sources_denied(self):
        from gateway.session_context import set_session_vars, clear_session_vars
        for source in ('cron', 'kanban', 'tool', 'oneshot'):
            tokens = set_session_vars(platform='', source=source, session_id='caller',
                                      profile='', cron_session='')
            try:
                self.assertIsNone(self.tools.caller(), source)
            finally:
                clear_session_vars(tokens)

    async def test_source_only_delegate_foreign_profile_and_cron_denied(self):
        from agent.delegation_context import delegated_child_context
        from gateway.session_context import _VAR_MAP
        self.bind(platform='', source='desktop', cron_session='')
        with delegated_child_context():
            self.assertIsNone(self.tools.caller())
        _VAR_MAP['HERMES_SESSION_PROFILE'].set('minecraft')
        self.assertIsNone(self.tools.caller())
        _VAR_MAP['HERMES_SESSION_PROFILE'].set('')
        _VAR_MAP['HERMES_CRON_SESSION'].set('1')
        self.assertIsNone(self.tools.caller())

    async def test_explicit_local_platform_cannot_hide_cron_marker(self):
        self.bind(platform='local', cron_session='1')
        self.assertIsNone(self.tools.caller())

    async def test_unbound_and_env_impersonation_rejected(self):
        with patch.dict('os.environ', {'HERMES_SESSION_ID': 'caller', 'HERMES_SESSION_PLATFORM': 'local'}):
            self.assertFalse(json.loads(await self.tools.handle({'action': 'recent'}))['ok'])

    async def test_foreign_profile_rejected(self):
        self.bind(profile='foreign')
        self.assertFalse(json.loads(await self.tools.handle({'action': 'recent'}))['ok'])

    async def test_telegram_needs_no_plugin_allowlist(self):
        self.bind(platform='telegram', user_id='123', chat_type='dm')
        self.assertTrue(json.loads(await self.tools.handle({'action': 'recent'}))['ok'])
        self.assertEqual(self.tools.caller(), 'profile:default')

    async def test_telegram_group_uses_host_admission_and_confirmation(self):
        self.bind(platform='telegram', user_id='123', chat_type='group')
        self.assertTrue(json.loads(await self.tools.handle({'action': 'recent'}))['ok'])

    async def test_status_shared_across_clients_not_sessions(self):
        self.bind(platform='desktop', session_id='desktop-chat')
        result = json.loads(await self.tools.handle({'action': 'recent', 'limit': 3}))
        self.assertTrue(result['ok'], result)
        self.bind(platform='api_server', session_id='api-chat', user_id='different')
        status = json.loads(await self.tools.handle({'action': 'status', 'job_id': result['job_id']}))
        self.assertEqual(status['state'], 'pending')
        self.bind(platform='telegram', session_id='telegram-chat', user_id='not-in-plugin-list')
        status = json.loads(await self.tools.handle({'action': 'status', 'job_id': result['job_id']}))
        self.assertEqual(status['state'], 'pending')

    async def test_api_caller_allowed_without_enabling_api_targets(self):
        self.bind(platform='api_server', session_id='api-chat')
        self.assertTrue(json.loads(await self.tools.handle({'action': 'recent', 'limit': 2}))['ok'])

    async def test_delegate_rejected(self):
        from agent.delegation_context import delegated_child_context
        self.bind()
        with delegated_child_context():
            self.assertFalse(json.loads(await self.tools.handle({'action': 'recent'}))['ok'])

    async def test_no_model_supplied_targets(self):
        self.bind()
        for args in ({'action': 'recent', 'profile': 'foreign'},
                     {'action': 'recent', 'session_id': 'foreign'},
                     {'action': 'recent', 'limit': True}):
            self.assertFalse(json.loads(await self.tools.handle(args))['ok'])


class ResidentTests(unittest.TestCase):
    def test_managed_worker_cannot_start_timer_consumer(self):
        with tempfile.TemporaryDirectory() as tmp:
            resident = Resident(Queue(tmp), lambda: None, 'default', lambda: True)
            with patch('agent.safe_worker_policy.worker_config_snapshot', return_value={}):
                self.assertFalse(resident.start())
            self.assertIsNone(resident.thread)

    def test_resident_outlives_hook_and_consumes_queue(self):
        done = threading.Event()
        class FakeEngine:
            consent = True
            async def decorate(self, profile, sid):
                done.set()
                return Outcome.UPDATED
            async def close(self):
                pass
        with tempfile.TemporaryDirectory() as tmp:
            queue = Queue(tmp)
            queue.schedule('first-hook-id', 0)
            resident = Resident(queue, FakeEngine, 'default', lambda: True)
            with patch('agent.safe_worker_policy.worker_config_snapshot', return_value=None):
                self.assertTrue(resident.start())
                duplicate = Resident(queue, FakeEngine, 'default', lambda: True)
                self.assertFalse(duplicate.start())
            try:
                self.assertTrue(done.wait(5))
            finally:
                resident.stop()
            self.assertFalse(resident.thread.is_alive())
            self.assertIsNone(queue.claim())


class LoaderTests(unittest.TestCase):
    def test_first_hook_through_resident_host_facade_and_native_receipt(self):
        from hermes_state import SessionDB
        from agent.plugin_llm import make_plugin_llm_for_test, _TrustPolicy
        from hermes_state_runtime import begin_runtime_epoch, mutate_runtime_session
        from session_title_icons.plugin import register
        with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
            home = Path(tmp)
            db = SessionDB(home / 'state.db')
            stack.callback(db.close)
            db.create_session('hook-session', 'cli')
            db.set_auto_title('hook-session', 'Synthetic backups', source='llm')
            epoch = begin_runtime_epoch(db, instance_id='isolated-hook-flow')
            committed = threading.Event()
            sent = []
            async def model(**kwargs):
                sent.append(kwargs)
                completion = SimpleNamespace(model='synthetic', choices=[SimpleNamespace(
                    message=SimpleNamespace(content='{"emoji":"💾"}'))], usage=SimpleNamespace(
                        prompt_tokens=10, completion_tokens=5, total_tokens=15))
                return 'synthetic-provider', 'synthetic', completion
            llm = make_plugin_llm_for_test(plugin_id='session-title-icons',
                policy=_TrustPolicy(plugin_id='session-title-icons'), async_caller=model)
            hooks, cleanups, tools = {}, [], {}
            settings = {'model_consent': True, 'single_owner_profile': True, 'delay_seconds': 0}
            ctx = SimpleNamespace(profile_name='custom', llm=llm,
                get_config=lambda key, default=None: settings.get(key, default),
                on_unload=cleanups.append,
                register_hook=lambda name, handler: hooks.update({name: handler}),
                register_tool=lambda name, toolset, schema, handler, **kw: tools.update({name: handler}))
            async def rpc(store, method, **params):
                result = mutate_runtime_session(db, epoch=epoch, principal_id='synthetic-owner', **params)
                committed.set()
                return result
            with patch('hermes_constants.get_hermes_home', return_value=home), \
                    patch('hermes_cli.gateway_runtime.gateway_profile_home', side_effect=lambda h: h), \
                    patch.object(HermesStore, '_rpc', rpc):
                try:
                    register(ctx)
                    hooks['on_session_start'](session_id='hook-session')
                    hooks['on_session_start'](session_id='hook-session')
                    self.assertTrue(committed.wait(8), 'resident did not commit synthetic title')
                finally:
                    for cleanup in reversed(cleanups):
                        cleanup()
            self.assertEqual(db.get_session_title('hook-session'), '💾 Synthetic backups')
            self.assertEqual(len(sent), 1)
            self.assertIn('session_title_icons', tools)
            # Built by the real host facade: the sole user input is the title.
            encoded = json.dumps(sent[0], default=str, ensure_ascii=False)
            self.assertIn('Synthetic backups', encoded)
            self.assertNotIn('hook-session', encoded)

    def test_real_directory_loader_registers_separate_inert_plugin(self):
        from hermes_cli.plugins import PluginManager
        from hermes_cli.plugins_manifest import parse_manifest_file
        root = Path(__file__).resolve().parents[1] / 'session_title_icons'
        with tempfile.TemporaryDirectory() as tmp:
            manager = PluginManager(scope_key=tmp)
            manifest = parse_manifest_file(root / 'plugin.yaml', root, 'user', '')
            manager._load_plugin(manifest)
            loaded = manager._plugins[manifest.name]
            try:
                self.assertTrue(loaded.enabled, loaded.error)
                self.assertIn('session_title_icons', manager._plugin_tool_names)
                self.assertNotIn('on_session_start', manager._hooks)
            finally:
                manager.unload(manifest.name)


if __name__ == '__main__':
    unittest.main()
