"""Every page has a Content-Security-Policy: scripts only from Altera's own files and the page's nonce-marked start-up
scripts, so an injected script or inline handler does not run."""
import re

import pytest

from core.app import create_app
from test_companion_api import tmp_store, world  # noqa: F401


@pytest.fixture
def client(world):
    return create_app(autostart=False).test_client()


def policy_of(response):
    header = response.headers.get("Content-Security-Policy")
    assert header, response.request.path
    return dict(part.strip().split(" ", 1) for part in header.split(";"))


@pytest.mark.parametrize("path", ["/", "/home", "/memory", "/story", "/search", "/lock", "/setup", "/knowledge", "/timeline"])
def test_each_page_refuses_scripts_that_are_not_altera_s_own_or_carry_its_nonce(client, path):
    response = client.get(path)
    if response.status_code in (301, 302):
        response = client.get(response.headers["Location"])
    assert response.status_code == 200, path
    policy = policy_of(response)
    nonce = re.search(r"'nonce-([^']+)'", policy["script-src"]).group(1)
    assert policy["script-src"] == f"'self' 'nonce-{nonce}'"               # no 'unsafe-inline', no 'unsafe-eval'
    assert policy["object-src"] == "'none'" and policy["frame-ancestors"] == "'none'" and policy["base-uri"] == "'none'"
    page = response.get_data(as_text=True)
    inline = re.findall(r"<script(?![^>]*\bsrc=)([^>]*)>", page)
    assert inline and all(f'nonce="{nonce}"' in attributes for attributes in inline), path
    assert not re.search(r"\son[a-z]+\s*=\s*[\"']", page), path              # nothing a policy would refuse


def test_the_nonce_is_new_for_each_page(client):
    first, second = (policy_of(client.get("/home"))["script-src"] for _ in range(2))
    assert first != second


def test_an_api_answer_has_no_page_policy_and_a_response_keeps_its_own(client):
    assert "Content-Security-Policy" not in client.get("/api/lock").headers
    from flask import Response
    from core.csp import install_content_security_policy
    from flask import Flask
    app = Flask(__name__)
    install_content_security_policy(app)
    app.add_url_rule("/own", "own", lambda: Response("<p>x</p>", mimetype="text/html",
                                                      headers={"Content-Security-Policy": "default-src 'none'"}))
    assert app.test_client().get("/own").headers["Content-Security-Policy"] == "default-src 'none'"
