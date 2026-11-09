"""Opt-in session-title emoji plugin."""
import math

from .engine import Engine
from .queue import Queue
from .runtime import Resident
from .selector import Selector
from .store import HermesStore
from .tools import DESCRIPTION, SCHEMA, Tools
from .unicode_emoji import Emoji


def register(ctx):
    def enabled():
        return (ctx.get_config('model_consent', False) is True
                and ctx.get_config('single_owner_profile', False) is True)

    # Remains discoverable but inert without BOTH explicit operator grants.
    if not enabled():
        async def disabled(args):
            return '{"ok":false,"error":"plugin_consent_required"}'
        ctx.register_tool('session_title_icons', 'session_title_icons', SCHEMA,
                          disabled, is_async=True, description=DESCRIPTION)
        return

    from hermes_constants import get_hermes_home, set_hermes_home_override
    from hermes_cli.gateway_runtime import gateway_profile_home
    from hermes_state import SessionDB
    home = gateway_profile_home(get_hermes_home()).resolve()
    if ctx.profile_name not in {'default', 'custom'}:
        raise ValueError('v0.1 supports only the single-owner default profile')
    profile = 'default'  # a custom HERMES_HOME is the deployment's default home
    delay = ctx.get_config('delay_seconds', 60)
    if isinstance(delay, bool) or not isinstance(delay, (int, float)) or not math.isfinite(delay) or not 0 <= delay <= 3600:
        raise ValueError('delay_seconds must be 0..3600')
    queue = Queue(home / 'session-title-icons')
    tools = Tools(queue, profile, enabled)

    def engine_factory():
        # Dedicated thread, no inherited request/turn identity. All model auth
        # remains with the host facade in this exact profile's home scope.
        set_hermes_home_override(str(home))
        emoji = Emoji()
        store = HermesStore(home, profile, allowed=enabled)
        return Engine(store, Selector(ctx.llm, consent=True, allowed=enabled),
                      is_emoji=emoji.is_emoji, first_grapheme=emoji.first,
                      sanitize=SessionDB.sanitize_title, consent=True)

    resident = Resident(queue, engine_factory, profile, enabled)
    ctx.on_unload(resident.stop)
    resident.start()

    def first_hook(session_id=None, **kwargs):
        from agent.delegation_context import is_delegated_child_process_context
        if enabled() and not is_delegated_child_process_context() and session_id:
            queue.schedule(session_id, delay)

    ctx.register_hook('on_session_start', first_hook)
    ctx.register_tool('session_title_icons', 'session_title_icons', SCHEMA,
                      tools.handle, is_async=True, description=DESCRIPTION)
