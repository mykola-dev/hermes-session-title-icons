"""Actual pinned host prompt queues/callbacks; human answers are synthetic.

No live client login, cloud model, production queue or rename is exercised.
"""
import contextlib
import asyncio
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from session_title_icons.confirmation import confirm_batch
from session_title_icons.queue import Queue
from session_title_icons.tools import Tools


class ConfirmationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from gateway.session_context import set_session_vars, clear_session_vars
        from tools import approval
        self.route = 'synthetic-title-confirmation'
        tokens = set_session_vars(platform='local', session_id='synthetic-caller',
                                  session_key=self.route, profile='', cron_session='')
        self.addCleanup(clear_session_vars, tokens)
        self.addCleanup(approval.unregister_gateway_notify, self.route)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.queue = Queue(Path(self.tmp.name) / 'queue')
        self.tools = Tools(self.queue, 'default', lambda: True)
        self.seen = []

    def gateway(self, choice):
        from tools import approval
        def notify(data):
            self.assertIsNone(self.queue.claim(), 'enqueue occurred before human answer')
            self.seen.append(data)
            self.assertEqual(approval.resolve_gateway_approval(
                self.route, choice, request_id='wrong-request'), 0)
            self.assertEqual(approval.resolve_gateway_approval(
                'foreign-route', choice, request_id=data['request_id']), 0)
            self.assertEqual(approval.resolve_gateway_approval(
                self.route, choice, request_id=data['request_id']), 1)
        approval.register_gateway_notify(self.route, notify)

    async def recent(self, limit=50):
        return json.loads(await self.tools.handle({'action': 'recent', 'limit': limit}))

    async def test_actual_gateway_queue_once_then_enqueue(self):
        self.gateway('once')
        result = await self.recent()
        self.assertTrue(result['ok'], result)
        self.assertEqual(self.queue.claim()['limit_n'], 50)
        prompt, = self.seen
        self.assertIn('50', prompt['command'])
        self.assertIs(prompt['allow_session'], False)
        self.assertIs(prompt['allow_permanent'], False)

    async def test_denial_and_broad_scopes_do_not_enqueue(self):
        for choice in ('deny', 'session', 'always', 'approve', '', None):
            self.gateway(choice)
            # None has no decision: use zero host timeout rather than waiting.
            with contextlib.ExitStack() as stack:
                if choice is None:
                    stack.enter_context(patch('tools.approval_context._get_approval_timeout', return_value=0))
                result = await self.recent()
            self.assertFalse(result['ok'], (choice, result))
            self.assertIsNone(self.queue.claim())

    async def test_yolo_and_allowlist_do_not_skip_prompt(self):
        self.gateway('deny')
        with patch.dict('os.environ', {'HERMES_YOLO': '1', 'HERMES_EXEC_ASK': '0'}), \
                patch('tools.approval.request_tool_approval', side_effect=AssertionError('auto gate used')):
            self.assertFalse((await self.recent())['ok'])
        self.assertEqual(len(self.seen), 1)

    async def test_actual_host_timeout_no_enqueue_and_prompt_removed(self):
        from tools import approval
        approval.register_gateway_notify(self.route, self.seen.append)
        with patch('tools.approval_context._get_approval_timeout', return_value=0):
            self.assertFalse((await self.recent())['ok'])
        self.assertEqual(approval.resolve_gateway_approval(self.route, 'once'), 0)
        self.assertIsNone(self.queue.claim())

    async def test_delivery_failure_no_enqueue(self):
        from tools import approval
        def fail(data):
            raise OSError('synthetic client disconnected')
        approval.register_gateway_notify(self.route, fail)
        self.assertFalse((await self.recent())['ok'])
        self.assertEqual(approval.resolve_gateway_approval(self.route, 'once'), 0)
        self.assertIsNone(self.queue.claim())

    async def test_missing_callback_fails_closed(self):
        with patch('tools.terminal_tool._get_approval_callback', return_value=None):
            self.assertFalse((await self.recent())['ok'])
        self.assertIsNone(self.queue.claim())

    async def test_each_call_gets_new_prompt_key(self):
        self.gateway('deny')
        await self.recent(30)
        await self.recent(50)
        self.assertNotEqual(self.seen[0]['pattern_key'], self.seen[1]['pattern_key'])
        self.assertNotEqual(self.seen[0]['request_id'], self.seen[1]['request_id'])

    async def test_invalid_args_cannot_request_confirmation(self):
        confirmer = AsyncMock(return_value=True)
        self.tools.confirmer = confirmer
        for args in ({'action': 'recent', 'limit': True},
                     {'action': 'recent', 'limit': 201},
                     {'action': 'recent', 'confirmed': True}):
            self.assertFalse(json.loads(await self.tools.handle(args))['ok'])
        confirmer.assert_not_awaited()

    async def test_revocation_while_confirming_no_enqueue(self):
        enabled = True
        async def confirm(limit):
            nonlocal enabled
            enabled = False
            return True
        self.tools.allowed = lambda: enabled
        self.tools.confirmer = confirm
        self.assertFalse((await self.recent())['ok'])
        self.assertIsNone(self.queue.claim())

    async def test_status_does_not_prompt(self):
        self.tools.confirmer = AsyncMock(side_effect=AssertionError('status prompted'))
        result = json.loads(await self.tools.handle({'action': 'status', 'job_id': 'unknown'}))
        self.assertEqual(result['error'], 'job_not_found')
        self.tools.confirmer.assert_not_awaited()

    async def test_binding_changed_while_confirming_no_enqueue(self):
        from gateway.session_context import _VAR_MAP
        async def confirm(limit):
            _VAR_MAP['HERMES_SESSION_KEY'].set('different-route-same-session-id')
            return True
        self.tools.confirmer = confirm
        self.assertFalse((await self.recent())['ok'])
        self.assertIsNone(self.queue.claim())

    async def test_client_detaches_after_answer_no_enqueue(self):
        from tools import approval
        def notify(data):
            approval.resolve_gateway_approval(self.route, 'once', request_id=data['request_id'])
            approval.unregister_gateway_notify(self.route)
        approval.register_gateway_notify(self.route, notify)
        self.assertFalse((await self.recent())['ok'])
        self.assertIsNone(self.queue.claim())

    async def test_parallel_calls_need_distinct_answers(self):
        from tools import approval
        lock = threading.Lock()
        def notify(data):
            with lock:
                self.seen.append(data)
                if len(self.seen) == 2:
                    self.assertNotEqual(self.seen[0]['pattern_key'], self.seen[1]['pattern_key'])
                    approval.resolve_gateway_approval(self.route, 'once', request_id=self.seen[0]['request_id'])
                    approval.resolve_gateway_approval(self.route, 'deny', request_id=self.seen[1]['request_id'])
        approval.register_gateway_notify(self.route, notify)
        results = await asyncio.gather(self.recent(30), self.recent(50))
        self.assertEqual(sum(r['ok'] for r in results), 1)
        self.assertIsNotNone(self.queue.claim())
        self.assertIsNone(self.queue.claim())

    async def test_cancelled_tool_never_enqueues_after_late_approval(self):
        from tools import approval
        loop = asyncio.get_running_loop()
        displayed = asyncio.Event()
        def notify(data):
            self.seen.append(data)
            loop.call_soon_threadsafe(displayed.set)
        approval.register_gateway_notify(self.route, notify)
        task = asyncio.create_task(self.recent())
        await asyncio.wait_for(displayed.wait(), timeout=3)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        # Native prompt is withdrawn too; a late answer cannot revive it.
        self.assertEqual(approval.resolve_gateway_approval(
            self.route, 'once', request_id=self.seen[0]['request_id']), 0)
        self.assertIsNone(self.queue.claim())

    async def test_cli_real_prompt_callback_once_only(self):
        def callback(command, description, **kwargs):
            self.assertIs(kwargs['allow_session'], False)
            self.assertIs(kwargs['allow_permanent'], False)
            return 'once'
        with patch('tools.terminal_tool._get_approval_callback', return_value=callback), \
                patch('tools.approval_context._no_user_can_answer', return_value=False):
            self.assertTrue(await confirm_batch(30))

    async def test_actual_ui_bridge_contract_and_server_request_response(self):
        from tools import approval
        from tui_gateway import server, server_requests
        sid = 'synthetic-confirmation-ui'
        frames = []
        def write(frame):
            frames.append(frame)
            self.assertEqual(frame['method'], 'approval')
            self.assertEqual(frame['params']['choices'], ['once', 'deny'])
            self.assertEqual(frame['params']['session_id'], sid)
            self.assertTrue(server_requests.resolve_response({
                'jsonrpc': '2.0', 'id': frame['id'], 'result': {'choice': 'once'}}))
        approval.register_gateway_notify(self.route, lambda data: server._emit_approval_request(sid, data))
        # Only network sinks and synthetic live record are supplied. Host payload
        # projection, typed request contract, request queue and response callbacks
        # all run unchanged. This does NOT test WebSocket login authorization.
        with patch.dict(server._sessions, {sid: {'session_key': self.route}}), \
                patch.object(server_requests, '_answerable', return_value=True), \
                patch.object(server_requests, '_write', write):
            result = await self.recent()
        self.assertTrue(result['ok'], result)
        self.assertEqual(len(frames), 1)

    async def test_actual_ui_bridge_old_client_refuses_without_enqueue(self):
        from tools import approval
        from tui_gateway import server, server_requests
        sid = 'synthetic-old-client-ui'
        approval.register_gateway_notify(self.route, lambda data: server._emit_approval_request(sid, data))
        with patch.dict(server._sessions, {sid: {'session_key': self.route}}), \
                patch.object(server_requests, '_answerable', return_value=False):
            self.assertFalse((await self.recent())['ok'])
        self.assertIsNone(self.queue.claim())

    async def test_unbound_route_cron_delegate_or_finite_do_not_prompt(self):
        from gateway.session_context import _VAR_MAP, _UNSET
        from agent.delegation_context import delegated_child_context
        self.gateway('once')
        for cron in ('1', _UNSET):
            _VAR_MAP['HERMES_CRON_SESSION'].set(cron)
            self.assertFalse(await confirm_batch(30))
        _VAR_MAP['HERMES_CRON_SESSION'].set('')
        with delegated_child_context():
            self.assertFalse(await confirm_batch(30))
        with patch('tools.approval_context._is_single_query_approval_context', return_value=True):
            self.assertFalse(await confirm_batch(30))
        _VAR_MAP['HERMES_SESSION_KEY'].set('')
        self.assertFalse(await confirm_batch(30))
        self.assertEqual(self.seen, [])
