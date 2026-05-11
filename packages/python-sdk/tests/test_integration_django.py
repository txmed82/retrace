"""Django integration smoke.

Configures a minimal Django project in-process — no DB, no apps — and
exercises a single URL through `RetraceMiddleware`.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

django = pytest.importorskip("django")

# Framework imports MUST come after `importorskip` so collection works
# on systems without django installed — hence E402 here is intentional.
from retrace_sdk import set_client  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def _configure_django():
    from django.conf import settings

    if not settings.configured:
        settings.configure(
            DEBUG=False,
            ALLOWED_HOSTS=["*"],
            ROOT_URLCONF=__name__,
            SECRET_KEY="test",
            MIDDLEWARE=[
                "retrace_sdk.integrations.django.RetraceMiddleware",
            ],
        )
    import django as dj

    dj.setup()


def _decode(body: bytes) -> dict[str, Any]:
    _, _, item_body = body.splitlines()
    return json.loads(item_body)


# Django URLConf — must live at module level. Imports are after the
# `_configure_django` fixture deliberately because the call sites here
# don't run until pytest has already called `settings.configure()`.
from django.urls import path  # noqa: E402
from django.http import HttpResponse  # noqa: E402


def _ok_view(request):
    return HttpResponse("ok")


def _boom_view(request):
    raise RuntimeError("django-kaboom")


urlpatterns = [
    path("ok", _ok_view),
    path("boom", _boom_view),
]


def test_django_captures_unhandled_exception(client_factory, fake_transport):
    from django.test import RequestFactory

    client = client_factory()
    set_client(client)

    factory = RequestFactory()
    request = factory.get("/boom")
    # Drive through middleware directly so we don't depend on the URL
    # resolver / view dispatch (which Django normally does behind ASGI/WSGI).
    from retrace_sdk.integrations.django import RetraceMiddleware

    def _dispatch(_request):
        raise RuntimeError("django-kaboom")

    mw = RetraceMiddleware(_dispatch)
    with pytest.raises(RuntimeError):
        mw(request)

    client.flush(timeout=1.0)
    assert len(fake_transport.sent) == 1
    event = _decode(fake_transport.sent[0]["body"])
    assert event["exception"]["values"][0]["type"] == "RuntimeError"
    assert event["exception"]["values"][0]["value"] == "django-kaboom"
    assert event["transaction"] == "GET /boom"


def test_django_ok_path_is_transparent(client_factory, fake_transport):
    from django.test import RequestFactory
    from retrace_sdk.integrations.django import RetraceMiddleware

    client = client_factory()
    set_client(client)

    def _ok(_request):
        return HttpResponse("hi")

    mw = RetraceMiddleware(_ok)
    response = mw(RequestFactory().get("/ok"))
    assert response.status_code == 200
    client.flush(timeout=1.0)
    assert fake_transport.sent == []


def test_django_no_active_client_is_no_op():
    """With the SDK disabled, middleware is transparent."""
    set_client(None)
    from django.test import RequestFactory
    from retrace_sdk.integrations.django import RetraceMiddleware

    def _ok(_request):
        return HttpResponse("hi")

    mw = RetraceMiddleware(_ok)
    response = mw(RequestFactory().get("/ok"))
    assert response.status_code == 200
