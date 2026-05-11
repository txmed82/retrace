"""Flask integration smoke."""

from __future__ import annotations

import json
from typing import Any

import pytest

flask = pytest.importorskip("flask")

# Framework imports MUST come after `importorskip` so collection works
# on systems without flask installed — hence E402 here is intentional.
from retrace_sdk import set_client  # noqa: E402
from retrace_sdk.integrations.flask import FlaskIntegration  # noqa: E402


def _decode(body: bytes) -> dict[str, Any]:
    _, _, item_body = body.splitlines()
    return json.loads(item_body)


def test_flask_captures_unhandled_exception(client_factory, fake_transport):
    from flask import Flask

    client = client_factory()
    set_client(client)

    app = Flask(__name__)
    app.config["TESTING"] = True
    # Flask's TESTING-mode reraises view errors out of the test client.
    # We don't want that here — we want the regular signal path to fire,
    # which it does whether or not the response is a 500.
    app.config["PROPAGATE_EXCEPTIONS"] = False

    @app.route("/boom")
    def _boom():
        raise RuntimeError("kapow")

    FlaskIntegration.attach(app)

    with app.test_client() as c:
        resp = c.get("/boom")
        assert resp.status_code == 500

    client.flush(timeout=1.0)
    assert len(fake_transport.sent) == 1
    event = _decode(fake_transport.sent[0]["body"])
    assert event["exception"]["values"][0]["type"] == "RuntimeError"
    assert event["exception"]["values"][0]["value"] == "kapow"


def test_flask_attach_is_idempotent(client_factory):
    from flask import Flask

    client = client_factory()
    set_client(client)
    app = Flask(__name__)
    FlaskIntegration.attach(app)
    FlaskIntegration.attach(app)  # must not raise
    assert getattr(app, "_retrace_sdk_attached", False) is True
