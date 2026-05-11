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


def test_flask_per_request_scope_isolation(client_factory, fake_transport):
    """Regression for CodeRabbit Major on PR #128: tags / user / extra
    set inside a request must not leak into the next request handled
    by the same worker context.
    """
    from flask import Flask
    from retrace_sdk import set_user, set_tag
    from retrace_sdk.scope import Scope

    client = client_factory()
    set_client(client)

    app = Flask(__name__)
    app.config["TESTING"] = True

    @app.route("/leak")
    def _leak():
        set_user({"id": "from-request"})
        set_tag("phase", "checkout")
        return "ok"

    @app.route("/probe")
    def _probe():
        snap = Scope.current().snapshot()
        # If the scope leaked, the prior request's user/tag would show up here.
        return {"user": snap["user"], "tags": snap["tags"]}

    FlaskIntegration.attach(app)

    with app.test_client() as c:
        assert c.get("/leak").status_code == 200
        # Second request must see a clean scope.
        probe_body = c.get("/probe").get_json()
        assert probe_body == {"user": {}, "tags": {}}, probe_body


def test_flask_attach_is_idempotent(client_factory):
    from flask import Flask

    client = client_factory()
    set_client(client)
    app = Flask(__name__)
    FlaskIntegration.attach(app)
    FlaskIntegration.attach(app)  # must not raise
    assert getattr(app, "_retrace_sdk_attached", False) is True
