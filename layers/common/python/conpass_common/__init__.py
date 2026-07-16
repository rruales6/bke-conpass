"""conpass shared Lambda layer.

Import surface for feature services. Keep this thin — heavy deps (supabase, jwt,
google libs) are imported lazily inside the modules that need them so cold starts of a
feature Lambda only pay for what it uses.
"""
from . import errors
from .app import CurrentIdentity, create_app, lambda_handler, require_identity
from .auth import Identity
from .config import settings

__all__ = [
    "create_app",
    "lambda_handler",
    "require_identity",
    "CurrentIdentity",
    "Identity",
    "settings",
    "errors",
]
