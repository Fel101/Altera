"""Keep documented routes, local links and rendered page assets in sync."""
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit

import pytest

from core.app import create_app

ROOT = Path(__file__).resolve().parent.parent


def test_documented_local_links_exist():
    broken = []
    for document in [ROOT / 'README.md', *sorted((ROOT / 'docs').glob('*.md'))]:
        text = document.read_text('utf-8')
        assert not any(ord(c) < 32 and c not in '\n\r\t' for c in text), document.name
        for target in re.findall(r'\[[^\]]*\]\(([^\s)]+)(?:\s+[^)]*)?\)', text):
            link = urlsplit(target.strip('<>'))
            if link.scheme or link.netloc or not link.path:
                continue
            if not (document.parent / unquote(link.path)).exists():
                broken.append((document.name, target))
    assert not broken, broken


def test_every_registered_application_route_has_a_documented_contract():
    app = create_app(autostart=False)
    reference = (ROOT / 'docs/API_REFERENCE.md').read_text('utf-8')
    missing = []
    for rule in app.url_map.iter_rules():
        if rule.endpoint == 'static':
            continue
        if rule.rule not in reference:
            missing.append(rule.rule)
    assert not missing, missing


@pytest.mark.parametrize('page', ['/', '/home', '/setup', '/memory', '/timeline', '/search', '/knowledge', '/story'])
def test_documented_pages_render_with_current_local_assets(page, monkeypatch):
    from core import companions
    from core.datastore import get_datastore
    from core.profile import complete_onboarding
    # Template checks must not initialize integrations or migrate an actual instance.
    sid = get_datastore().create_session('Template check')
    complete_onboarding()  # /home sends an unfinished first run to /setup
    monkeypatch.setattr(companions, 'migrate_legacy', dict)
    monkeypatch.setattr(companions, 'migrate_kinds', lambda: 0)
    monkeypatch.setattr(companions, 'current', lambda: {'session_id': sid})
    app = create_app(autostart=False)
    response = app.test_client().get(page)
    assert response.status_code == 200
    assert response.headers['Cache-Control'] == 'no-store'
    html = response.get_data(as_text=True)
    assert '/static/js/browser-state.js' in html
    if 'AlteraStorage' in html:
        assert html.index('/static/js/browser-state.js') < html.index('AlteraStorage')
    for path in re.findall(r'(?:src|href)=["\'](/static/[^"\'?]+)', html):
        assert (ROOT / path.lstrip('/')).is_file(), (page, path)


def test_browser_consumers_cannot_bypass_private_readiness_with_native_storage():
    # Adding another native-storage consumer could leak private page content
    # before authenticated keys exist. Only the explicit facade owns access.
    for source in [*sorted((ROOT / 'static/js').glob('*.js')), *sorted((ROOT / 'templates').glob('*.html'))]:
        if source.name == 'browser-state.js':
            continue
        text = source.read_text('utf-8')
        assert not re.search(r'\b(?:localStorage|sessionStorage)\b', text), source.name
