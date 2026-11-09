"""Enqueue bounded, same-profile batches; never accept profile/session targets."""
import asyncio
import json

DESCRIPTION = (
    'Set session icons by ADDING one emoji before existing session titles; these are title prefixes, not platform custom icons. '
    'Never rewrite the title text or replace an existing leading emoji. '
    'Only on explicit user request: action=recent, limit=N (1..200), default30. '
    'Select the last N sessions by activity, then skip untitled/already decorated sessions without backfill. '
    'Requires a fresh human confirmation before enqueue; never ask for a confirmed=true argument. '
    'Returns a queued job_id, NOT completed work. Use action=status with that job_id to get counts. '
    'Do not claim icons were set until status is done and updated count is positive.'
)
SCHEMA = {'name': 'session_title_icons', 'description': DESCRIPTION, 'parameters': {
    'type': 'object', 'additionalProperties': False,
    'properties': {'action': {'type': 'string', 'enum': ['recent', 'status']},
                   'limit': {'type': 'integer', 'minimum': 1, 'maximum': 200},
                   'job_id': {'type': 'string'}}, 'required': ['action']}}


class Tools:
    def __init__(self, queue, profile, allowed, *, confirmer=None):
        from .confirmation import confirm_batch
        self.queue, self.profile, self.allowed = queue, profile, allowed
        self.confirmer = confirm_batch if confirmer is None else confirmer

    def caller(self):
        # Routing/isolation only, NOT authentication. Trust Hermes admission;
        # every batch still requires its own human confirmation.
        from gateway.session_context import _VAR_MAP
        from agent.delegation_context import is_delegated_child_process_context
        if is_delegated_child_process_context():
            return None
        fields = ('ID', 'PLATFORM', 'SOURCE', 'PROFILE')
        principal = {field: _VAR_MAP['HERMES_SESSION_' + field].get() for field in fields}
        cron = _VAR_MAP['HERMES_CRON_SESSION'].get()
        if cron != '':
            return None
        sid = principal['ID']
        profile = 'default' if principal['PROFILE'] == '' else principal['PROFILE']
        if (not isinstance(sid, str) or not sid or len(sid) > 256 or profile != self.profile):
            return None
        if any(principal[field] in {'cron', 'kanban', 'tool', 'oneshot'}
               for field in ('PLATFORM', 'SOURCE') if isinstance(principal[field], str)):
            return None
        # Shared across admitted clients of this home; old session-scoped jobs
        # are not migrated or replayed.
        return 'profile:' + self.profile

    async def handle(self, args):
        try:
            caller = self.caller()
            if not self.allowed() or caller is None:
                result = {'ok': False, 'error': 'interactive_profile_required'}
            elif not isinstance(args, dict) or args.get('action') not in {'recent', 'status'}:
                result = {'ok': False, 'error': 'invalid_arguments'}
            elif args['action'] == 'recent':
                if set(args) - {'action', 'limit'}:
                    raise ValueError('invalid_arguments')
                limit = args.get('limit', 30)
                if type(limit) is not int or not 1 <= limit <= 200:
                    raise ValueError('invalid_arguments')
                from gateway.session_context import _VAR_MAP
                fields = ('ID', 'KEY', 'PROFILE', 'USER_ID', 'PLATFORM', 'SOURCE', 'CHAT_TYPE')
                binding = tuple(_VAR_MAP['HERMES_SESSION_' + field].get() for field in fields)
                if await self.confirmer(limit) is not True:
                    result = {'ok': False, 'error': 'confirmation_not_received'}
                elif (not self.allowed() or self.caller() != caller
                      or tuple(_VAR_MAP['HERMES_SESSION_' + field].get() for field in fields) != binding):
                    result = {'ok': False, 'error': 'interactive_profile_required'}
                else:
                    # No suspension between final consent/binding check and
                    # insert: cancellation must not leave an enqueueing thread.
                    job_id = self.queue.batch(caller, limit)
                    result = {'ok': True, 'state': 'queued', 'job_id': job_id, 'selected_limit': limit}
            else:
                if (set(args) != {'action', 'job_id'} or not isinstance(args['job_id'], str)
                        or not 0 < len(args['job_id']) <= 64):
                    raise ValueError('invalid_arguments')
                status = await asyncio.to_thread(self.queue.status, args['job_id'], caller)
                result = {'ok': True, **status} if status else {'ok': False, 'error': 'job_not_found'}
        except ValueError:
            result = {'ok': False, 'error': 'invalid_arguments'}
        except Exception:
            result = {'ok': False, 'error': 'tool_unavailable'}
        return json.dumps(result)
