"""Once-only host human prompt, not the auto-approvable tool approval gate.

Pinned host APIs are read/called, never replaced. Only an exact `once` answer
permits enqueue. No allowlist/YOLO/smart approval or stdin fallback is consulted.
"""
import asyncio
import threading
from uuid import uuid4


async def confirm_batch(limit):
    from agent.delegation_context import is_delegated_child_process_context
    from gateway.session_context import _VAR_MAP
    from tools import approval, approval_context
    from tools.terminal_tool import _get_approval_callback

    if type(limit) is not int or not 1 <= limit <= 200:
        return False
    if is_delegated_child_process_context():
        return False
    # Exact task binding, no environment fallback for the target of the prompt.
    route = _VAR_MAP['HERMES_SESSION_KEY'].get()
    cron = _VAR_MAP['HERMES_CRON_SESSION'].get()
    fields = ('HERMES_SESSION_ID', 'HERMES_SESSION_KEY', 'HERMES_SESSION_PROFILE',
              'HERMES_SESSION_USER_ID', 'HERMES_SESSION_PLATFORM', 'HERMES_SESSION_SOURCE',
              'HERMES_CRON_SESSION')
    binding = tuple(_VAR_MAP[name].get() for name in fields)
    if not isinstance(route, str) or not route or cron != '':
        return False
    if approval_context._is_single_query_approval_context():
        return False

    # Capture a CLI/UI thread-local callback BEFORE moving blocking work to a
    # helper thread. The helper inherits ContextVars, not threading.local state.
    notify = approval._gateway_notify_cb(route)
    callback = _get_approval_callback() if notify is None else None
    if notify is None and (not callable(callback) or approval_context._no_user_can_answer()):
        return False

    message = f'Додати емоджі до назв останніх {limit} користувацьких чатів?'
    description = ('Текст назв не змінюється. Неназвані та вже оформлені чати буде пропущено. '
                   'Дозвіл лише на цей batch; до підтвердження завдання не створюється.')
    # Unique key prevents concurrent requests coalescing onto one decision.
    request_id = uuid4().hex
    key = 'session_title_icons:batch:' + request_id
    cancelled = threading.Event()

    def withdraw():
        approval.withdraw_gateway_approval(route, request_id, 'batch tool cancelled')

    def notify_current(data):
        # Covers cancellation BEFORE the helper has registered its host prompt,
        # as well as a cancellation racing the client notification itself.
        if cancelled.is_set():
            withdraw()
            raise RuntimeError('batch_confirmation_cancelled')
        notify(data)
        if cancelled.is_set():
            withdraw()

    def wait():
        if cancelled.is_set():
            return False
        if notify is not None:
            from tools.approval_gateway_wait import _await_gateway_decision
            decision = _await_gateway_decision(route, notify_current, {
                'command': message, 'description': description,
                'request_id': request_id,
                'pattern_key': key, 'pattern_keys': [key],
                'allow_session': False, 'allow_permanent': False,
            }, surface='session-title-icons')
            return (isinstance(decision, dict) and decision.get('resolved') is True
                    and not decision.get('cancelled') and not decision.get('notify_failed')
                    and decision.get('choice') == 'once')
        from tools.approval_prompt import prompt_dangerous_approval
        return prompt_dangerous_approval(
            message, description, allow_session=False, allow_permanent=False,
            approval_callback=callback, title='Підтвердити оформлення чатів?') == 'once'

    try:
        accepted = await asyncio.to_thread(wait)
        if tuple(_VAR_MAP[name].get() for name in fields) != binding:
            return False
        if notify is not None and approval._gateway_notify_cb(route) is not notify:
            return False
        return accepted
    except asyncio.CancelledError:
        cancelled.set()
        if notify is not None:
            withdraw()
        raise
    except Exception:
        return False
