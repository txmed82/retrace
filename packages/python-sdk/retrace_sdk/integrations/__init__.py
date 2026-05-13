"""Framework integrations.

Each integration is a class with two responsibilities:

  1. `setup(client)` — register hooks/middleware so unhandled exceptions
     reach `client.capture_exception()` automatically.
  2. Be importable without its target framework installed — the import
     of the framework happens *inside* `setup()`, so a user who only
     uses FastAPI doesn't pay an import cost for Django.

Inspired by Sentry-Python's `Integration` base, but without the global
auto-discovery — every integration is explicit at `init(integrations=[…])`.
"""

from ._base import Integration

__all__ = ["Integration"]
