"""Transport-independent scheduler and batch engine.

The Hermes adapter uses an immediate reread and revision/generation-fenced
rename. Direct native title writes can still race the final reread: this is the
explicitly owner-accepted best-effort contract, NOT atomic title CAS.
"""

from __future__ import annotations

import asyncio
import logging
import traceback
from dataclasses import dataclass
from enum import Enum
from typing import Awaitable, Callable, Protocol


@dataclass(frozen=True)
class Snapshot:
    profile: str
    session_id: str
    title: str | None
    revision: int
    generation: int
    last_active: float
    eligible: bool = True


class Store(Protocol):
    async def read(self, profile: str, session_id: str) -> Snapshot | None: ...

    async def recent(self, profile: str, limit: int) -> list[Snapshot]:
        """Return latest visible user sessions by activity, newest first."""
        ...

    async def rename_if_unchanged(self, before: Snapshot, title: str) -> bool:
        """Reread exact profile/id/title/revision/generation/eligibility, then rename.

        Use native mutation publication on success. Never revive a deleted
        row, follow a reset/compression descendant, or retry with fresh fences.
        """
        ...


class Outcome(str, Enum):
    UPDATED = 'updated'
    SKIPPED = 'skipped'
    STALE = 'stale'
    ERROR = 'error'


class Engine:
    def __init__(self, store: Store, select: Callable[[str], Awaitable[str]], *,
                 is_emoji: Callable[[str], bool], first_grapheme: Callable[[str], str],
                 sanitize: Callable[[str], str | None], delay_seconds: float = 60,
                 timeout_seconds: float = 60, concurrency: int = 2,
                 max_sessions: int = 200, consent: bool = False):
        if not 0 <= delay_seconds <= 3600:
            raise ValueError('delay_seconds must be 0..3600')
        if not 0 < timeout_seconds <= 300:
            raise ValueError('timeout_seconds must be >0..300')
        if type(concurrency) is not int or not 1 <= concurrency <= 8:
            raise ValueError('concurrency must be 1..8')
        if type(max_sessions) is not int or not 1 <= max_sessions <= 10000:
            raise ValueError('max_sessions must be 1..10000')
        self.store, self.select = store, select
        self.is_emoji, self.first_grapheme, self.sanitize = is_emoji, first_grapheme, sanitize
        self.delay, self.timeout = delay_seconds, timeout_seconds
        self.consent = consent
        self.semaphore = asyncio.Semaphore(concurrency)
        self.max_sessions = max_sessions
        self.seen: set[tuple[str, str]] = set()
        self.pending: dict[tuple[str, str], asyncio.Task] = {}
        self.busy: set[tuple[str, str]] = set()
        self.closed = False

    def schedule(self, profile: str, session_id: str) -> bool:
        """Call only on the service loop, never in a transient worker thread.

        A bounded, process-lifetime seen set deliberately refuses new schedules
        when full rather than evicting IDs and silently scheduling them twice.
        No model call occurs on this hook path.
        """
        key = (profile, session_id)
        if (self.closed or not self.consent or not session_id or key in self.seen
                or len(self.seen) >= self.max_sessions):
            return False
        loop = asyncio.get_running_loop()
        self.seen.add(key)
        task = loop.create_task(self._delayed(profile, session_id))
        self.pending[key] = task
        task.add_done_callback(lambda _task: self.pending.pop(key, None))
        return True

    async def _delayed(self, profile: str, session_id: str) -> Outcome:
        await asyncio.sleep(self.delay)
        return await self.decorate(profile, session_id)

    async def decorate(self, profile: str, session_id: str) -> Outcome:
        key = (profile, session_id)
        if self.closed or not self.consent or key in self.busy:
            return Outcome.SKIPPED
        self.busy.add(key)
        stage = 'read'
        try:
            async with self.semaphore, asyncio.timeout(self.timeout):
                before = await self.store.read(profile, session_id)
                if (before is None or before.profile != profile or before.session_id != session_id
                        or not before.eligible or not before.title or not before.title.strip()
                        or self.is_emoji(self.first_grapheme(before.title))):
                    return Outcome.SKIPPED
                # Only the current title crosses the model boundary. Treat it as
                # untrusted data in the selector; output must be exactly one emoji.
                stage = 'select'
                icon = await self.select(before.title)
                if not self.is_emoji(icon) or self.first_grapheme(icon) != icon:
                    return Outcome.ERROR
                title = f'{icon} {before.title}'
                # Do not let host sanitation split ZWJ emoji, normalize a manual
                # name, or truncate its text. No restricted selection catalog.
                stage = 'sanitize'
                if self.sanitize(title) != title:
                    return Outcome.SKIPPED
                if self.closed or not self.consent:
                    return Outcome.SKIPPED
                stage = 'rename'
                return (Outcome.UPDATED if await self.store.rename_if_unchanged(before, title)
                        else Outcome.STALE)
        except Exception as exc:
            category = next((kind.__name__ for kind in
                             (TimeoutError, ImportError, AttributeError, TypeError,
                              ValueError, PermissionError, RuntimeError)
                             if isinstance(exc, kind)), 'Exception')
            frames = traceback.extract_tb(exc.__traceback__)
            # Code coordinates only; never source lines, exception text or locals.
            origin = ','.join(f'{frame.name}:{frame.lineno}' for frame in frames[-4:])
            logging.getLogger(__name__).warning(
                'session_title_failed stage=%s category=%s origin=%s', stage, category, origin)
            # Do not log model output, titles, credentials or message contents.
            # CancelledError is BaseException and must propagate to shutdown.
            return Outcome.ERROR
        finally:
            self.busy.discard(key)

    async def recent(self, profile: str, limit: int) -> dict[str, int]:
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError('limit must be 1..200')
        counts = {outcome.value: 0 for outcome in Outcome}
        if self.closed or not self.consent:
            return counts
        # Select N first; skips do not cause older rows to be backfilled.
        rows = await self.store.recent(profile, limit)
        selected = []
        ids = set()
        for row in rows:
            if row.profile != profile:
                raise ValueError('adapter crossed the profile boundary')
            if row.session_id not in ids:
                selected.append(row)
                ids.add(row.session_id)
            if len(selected) == limit:
                break
        results = await asyncio.gather(*(self.decorate(profile, row.session_id) for row in selected))
        for result in results:
            counts[result.value] += 1
        return counts

    async def close(self) -> None:
        self.closed = True
        tasks = list(self.pending.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.pending.clear()
