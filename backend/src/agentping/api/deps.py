"""FastAPI dependencies."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated, Concatenate

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from agentping.auth.principal import Principal
from agentping.auth.service import Authenticator
from agentping.domain.errors import Unauthenticated
from agentping.logging import request_id_var
from agentping.services.runtime import Runtime, ServiceContext

bearer = HTTPBearer(auto_error=False, description="Agent, device, OIDC or development token")


def get_runtime(request: Request) -> Runtime:
    runtime: Runtime = request.app.state.runtime
    return runtime


def get_authenticator(request: Request) -> Authenticator:
    authenticator: Authenticator = request.app.state.authenticator
    return authenticator


async def get_principal(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    authenticator: Annotated[Authenticator, Depends(get_authenticator)],
) -> Principal:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise Unauthenticated("Missing bearer token")
    return await authenticator.authenticate(credentials.credentials)


async def optional_principal(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    authenticator: Annotated[Authenticator, Depends(get_authenticator)],
) -> Principal | None:
    if credentials is None:
        return None
    return await authenticator.authenticate(credentials.credentials)


class Caller:
    """Runs a service call in its own transaction, committed before the response is sent."""

    def __init__(self, runtime: Runtime, principal: Principal) -> None:
        self.runtime = runtime
        self.principal = principal

    async def __call__[**P, T](
        self,
        operation: Callable[Concatenate[ServiceContext, P], Awaitable[T]],
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> T:
        async with self.runtime.unit(self.principal, request_id=request_id_var.get()) as ctx:
            return await operation(ctx, *args, **kwargs)


def get_caller(
    runtime: Annotated[Runtime, Depends(get_runtime)],
    principal: Annotated[Principal, Depends(get_principal)],
) -> Caller:
    return Caller(runtime, principal)


Call = Annotated[Caller, Depends(get_caller)]
