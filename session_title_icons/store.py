"""Read-only host metadata + official native gateway mutation RPC.

No host DB writes, SQL, method replacement, worker claims or messages access.
"""
import asyncio
from pathlib import Path
from uuid import uuid4

from .engine import Snapshot


class HermesStore:
    def __init__(self, home, profile, *, db_factory=None, rpc=None, allowed=lambda: False):
        self.home, self.profile = Path(home).resolve(), profile
        if db_factory is None:
            from hermes_state import SessionDB
            db_factory = lambda: SessionDB(self.home / 'state.db', read_only=True)
        self.db_factory = db_factory
        self.rpc = rpc or self._rpc
        self.allowed = allowed

    def _snapshot(self, row):
        if row is None:
            return None
        from hermes_state_sessions import INTERNAL_LISTING_SOURCES
        eligible = (not row.get('hidden')
                    and row.get('source') not in {*INTERNAL_LISTING_SOURCES, 'cron', 'kanban', 'api_server'}
                    and row.get('profile_name') in (None, '', self.profile))
        return Snapshot(self.profile, row['id'], row.get('title'),
                        row['runtime_revision'], row['runtime_generation'],
                        row.get('last_active') or row.get('started_at') or 0, eligible)

    def _read(self, sid):
        db = self.db_factory()
        try:
            return self._snapshot(db.get_session(sid))
        finally:
            db.close()

    async def read(self, profile, session_id):
        if profile != self.profile:
            raise PermissionError('profile_mismatch')
        return await asyncio.to_thread(self._read, session_id)

    def _recent(self, limit):
        from hermes_state_sessions import INTERNAL_LISTING_SOURCES
        db = self.db_factory()
        try:
            rows = db.list_sessions_rich(
                limit=limit, order_by_last_active=True, compact_rows=True,
                include_hidden=False, include_archived=True,
                exclude_sources=[*INTERNAL_LISTING_SOURCES, 'cron', 'kanban', 'api_server'])
            # Do not backfill ineligible rows or rows with existing icons.
            return [self._snapshot(row) for row in rows]
        finally:
            db.close()

    async def recent(self, profile, limit):
        if profile != self.profile:
            raise PermissionError('profile_mismatch')
        return await asyncio.to_thread(self._recent, limit)

    async def rename_if_unchanged(self, before, title):
        latest = await self.read(before.profile, before.session_id)
        if latest is None or not latest.eligible or not before.eligible:
            return False
        fields = ('profile', 'session_id', 'title', 'revision', 'generation')
        if any(getattr(latest, field) != getattr(before, field) for field in fields):
            return False
        if not self.allowed():
            return False
        # This await starts the remaining best-effort race window. Never refresh
        # fences and retry a refused mutation against a different title/version.
        try:
            result = await self.rpc('session.mutate', session_id=before.session_id,
                request_id=str(uuid4()), expected_revision=before.revision,
                expected_generation=before.generation, operation='rename', payload={'title': title})
        except Exception as exc:
            from hermes_cli.gateway_client import GatewayRPCError
            if isinstance(exc, GatewayRPCError) and str(exc) in {
                    'revision_conflict', 'stale_generation', 'not_found', 'session_superseded'}:
                return False
            raise
        if not isinstance(result, dict):
            raise ValueError('invalid mutation response')
        return True

    async def _rpc(self, method, **params):
        from hermes_cli.gateway_client import connect_gateway
        from hermes_cli.gateway_runtime import discover_gateway_endpoint
        discovery = await asyncio.to_thread(discover_gateway_endpoint, self.home, timeout=1.5)
        if (discovery.state != 'ready' or discovery.endpoint is None
                or Path(discovery.endpoint.profile_id).resolve() != self.home):
            raise RuntimeError('owner_unavailable')
        # Explicit endpoint: never starts/restarts an owner or changes credentials.
        # Browser-created LOCAL sessions belong to the authenticated browser
        # principal, not the Unix socket UID. The host's same-user native client
        # explicitly supports acting for that stored principal. Never take a
        # subject/profile from model arguments. Single-owner opt-in is required.
        subject = await asyncio.to_thread(self._owner_subject, params['session_id'])
        async with connect_gateway(endpoint=discovery.endpoint, subject=subject) as client:
            return await client.rpc(method, **params)

    def _owner_subject(self, sid):
        db = self.db_factory()
        try:
            row = db.get_session(sid)
            if row and row.get('source') in {'local', 'cli', 'tui', 'desktop', 'gui'}:
                from gateway.session_identity import authenticated_subject, parse_authenticated_subject
                subject = row.get('user_id')
                if not subject:
                    return None
                if not isinstance(subject, str):
                    raise PermissionError('unsupported_owner_identity')
                if parse_authenticated_subject(subject) is not None:
                    return subject
                # Desktop stores issuer-less Basic login as "basic:<user>".
                # Bootstrap accepts ONLY the structured authenticated subject,
                # never this display label. This is the exact reverse of the
                # host's _login_label, not a newly invented owner/principal.
                # Keep conversion narrowly scoped; do not guess other providers.
                if row['source'] in {'desktop', 'gui'} and subject.startswith('basic:'):
                    user = subject[len('basic:'):]
                    if not user or user != user.strip():
                        raise PermissionError('unsupported_owner_identity')
                    canonical = authenticated_subject({'provider': 'basic', 'issuer': '', 'user_id': user})
                    if len(canonical) > 256:
                        raise PermissionError('unsupported_owner_identity')
                    return canonical
                if subject.startswith(('uid:', 'sid:')):
                    # Never forward or relabel a native peer identity. The
                    # private socket establishes the actual same-user principal.
                    return None
                if row['source'] == 'desktop' and subject.startswith('nous:'):
                    # The pinned host treats old Desktop Nous account metadata
                    # as a label, not an authenticated owner (legacy_account_label).
                    return None
                raise PermissionError('unsupported_owner_identity')
            return None
        finally:
            db.close()
