"""Shared FastAPI app factory + Lambda handler wrapper.

Every SRP service builds its app with `create_app(...)` and exports
`handler = lambda_handler(app)`. This centralizes error mapping, CORS, request IDs and
the auth dependency so each feature Lambda stays tiny.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import Depends, FastAPI, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .auth import Identity, identity_from_header
from .errors import AppError
from .logging import get_logger

log = get_logger("conpass.app")


def create_app(*, service: str, version: str = "0.1.0") -> FastAPI:
    app = FastAPI(title=f"conpass-{service}", version=version)

    # PWA (https://conpass.cards) is a different origin; origins come from config.
    from .config import settings

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError):
        return JSONResponse(status_code=exc.status_code, content=exc.to_body())

    @app.get("/health", tags=["health"])
    async def health():
        return {"status": "ok", "service": service, "version": version}

    return app


async def require_identity(
    authorization: Annotated[str | None, Header()] = None,
) -> Identity:
    """FastAPI dependency: resolve and require an authenticated identity."""
    return identity_from_header(authorization)


CurrentIdentity = Annotated[Identity, Depends(require_identity)]


def lambda_handler(app: FastAPI):
    """Wrap a FastAPI app as an API Gateway (HTTP API v2) Lambda handler via Mangum."""
    from mangum import Mangum

    return Mangum(app, lifespan="off")
