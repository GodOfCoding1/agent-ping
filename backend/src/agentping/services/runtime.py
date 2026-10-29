"""Runtime dependencies and the transactional unit of work shared by REST, MCP and worker."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentping.auth.principal import Principal
from agentping.config import Settings
from agentping.db.models import User, UserQuota
from agentping.domain.enums import ActionSource, ActorKind
from agentping.domain.errors import NotFound, RateLimited
from agentping.push.base import PushSender

logger = logging.getLogger(__name__)


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class RateLimiter:
    """In-process token bucket per actor. Durable per-agent quotas are enforced in services."""

    def __init__(self, per_minute: int, burst: int) -> None:
        self._rate = per_minute / 60.0
        self._burst = float(max(burst, 1))
        self._buckets: dict[str, tuple[float, float]] = {}

    def check(self, key: str, cost: float = 1.0) -> None:
        if self._rate <= 0:
            return
        now = time.monotonic()
        tokens, updated = self._buckets.get(key, (self._burst, now))
        tokens = min(self._burst, tokens + (now - updated) * self._rate)
        if tokens < cost:
            retry_after = (cost - tokens) / self._rate
            self._buckets[key] = (tokens, now)
            raise RateLimited(
                "Too many requests; slow down",
                details={"retry_after_seconds": round(retry_after, 1)},
            )
        self._buckets[key] = (tokens - cost, now)
        if len(self._buckets) > 10_000:
            self._buckets.clear()


class WorkerSignal:
    """Wakes an in-process worker after commits that enqueue work."""

    def __init__(self) -> None:
        self._event = asyncio.Event()

    def notify(self) -> None:
        self._event.set()

    async def wait(self, timeout: float) -> bool:
        try:
            await asyncio.wait_for(self._event.wait(), timeout=timeout)
            return True
        except TimeoutError:
            return False
        finally:
            self._event.clear()


@dataclass
class Runtime:
    settings: Settings
    session_factory: async_sessionmaker[AsyncSession]
    push: PushSender
    clock: Clock = field(default_factory=SystemClock)
    rate_limiter: RateLimiter | None = None
    signal: WorkerSignal = field(default_factory=WorkerSignal)
    after_commit_hooks: list[Callable[[], None]] = field(default_factory=list)
    work_notifier: Callable[[], Awaitable[None]] | None = None

    def __post_init__(self) -> None:
        if self.rate_limiter is None:
            self.rate_limiter = RateLimiter(
                self.settings.rate_limit_per_minute, self.settings.rate_limit_burst
            )

    @asynccontextmanager
    async def unit(
        self,
        principal: Principal,
        *,
        source: ActionSource = ActionSource.API,
        request_id: str | None = None,
        rate_limit: bool = True,
    ) -> AsyncIterator[ServiceContext]:
        if rate_limit and principal.actor_kind is not ActorKind.SYSTEM and self.rate_limiter:
            self.rate_limiter.check(principal.actor_key)
        async with self.session_factory() as session:
            ctx = ServiceContext(
                session=session,
                principal=principal,
                runtime=self,
                source=source,
                request_id=request_id,
                now=self.clock.now(),
            )
            async with session.begin():
                if rate_limit and principal.actor_kind is not ActorKind.SYSTEM:
                    # Durable per-user bound, shared by REST, MCP, browser sessions and
                    # all API instances. Token/client rotation cannot reset the quota.
                    window = ctx.now.replace(second=0, microsecond=0)
                    statement = insert(UserQuota).values(
                        user_id=principal.user_id, window_start=window, requests=1
                    )
                    used = (
                        await session.execute(
                            statement.on_conflict_do_update(
                                index_elements=[UserQuota.user_id, UserQuota.window_start],
                                set_={"requests": UserQuota.requests + 1},
                            ).returning(UserQuota.requests)
                        )
                    ).scalar_one()
                    if used > self.settings.user_requests_per_minute:
                        raise RateLimited(
                            "Account request limit reached",
                            details={
                                "retry_after_seconds": 60 - ctx.now.second,
                            },
                        )
                yield ctx
            if ctx.enqueued_work:
                self.signal.notify()
                for hook in self.after_commit_hooks:
                    hook()
                if self.work_notifier and source is not ActionSource.SYSTEM:
                    try:
                        await self.work_notifier()
                    except Exception as error:
                        # The transaction is already committed. Recovery will rediscover the
                        # durable outbox; reporting a write failure would invite duplicate writes.
                        logger.error(
                            "worker_wakeup_failed", extra={"error_type": type(error).__name__}
                        )


@dataclass
class ServiceContext:
    session: AsyncSession
    principal: Principal
    runtime: Runtime
    source: ActionSource
    request_id: str | None
    now: datetime
    enqueued_work: bool = False
    _locked_user: bool = False

    @property
    def settings(self) -> Settings:
        return self.runtime.settings

    @property
    def user_id(self) -> uuid.UUID:
        return self.principal.user_id

    async def lock_user(self) -> User:
        """Serialize all mutations for this user (gap-free, ordered sync sequence)."""
        user = (
            await self.session.execute(
                select(User).where(User.id == self.user_id).with_for_update()
            )
        ).scalar_one_or_none()
        if user is None:
            raise NotFound("User not found")
        self._locked_user = True
        return user

    async def user(self) -> User:
        user = await self.session.get(User, self.user_id)
        if user is None:
            raise NotFound("User not found")
        return user

    async def notify_worker(self) -> None:
        self.enqueued_work = True
        await self.session.execute(text("SELECT pg_notify('agentping_outbox', '')"))


def system_principal(user_id: uuid.UUID) -> Principal:
    return Principal(
        user_id=user_id,
        actor_kind=ActorKind.SYSTEM,
        scopes=frozenset(),
        auth_method="system",
    )
