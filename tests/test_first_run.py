"""First-run setup: the check that tells a starter config from a working chat, and the page that ends on Home.

The model is always a fake: `check` takes the call it makes as an argument, and the route test replaces it.
"""
import pytest

from core import setup
from core.setup import check
from core.routes import setup as setup_routes
from test_blueprints import create_app
from test_companion_api import tmp_store, world  # noqa: F401


def config(model='local-chat', provider='lmstudio', **providers):
    return {'llm': {'provider': provider, 'model': model, 'providers': providers}}


class Refused(Exception):
    """A provider that answers with an HTTP status, like the OpenAI client's errors."""

    def __init__(self, status_code, message='no'):
        super().__init__(message)
        self.status_code = status_code


# ---------------------------------------------------------------- the check

def test_the_check_reports_a_model_that_answers():
    asked = []

    def ask(provider, model):
        asked.append((provider.base_url, model))
        return 'OK'

    result = check(config(), ask=ask)

    assert result['ok'] is True and result['reply'] == 'OK'
    assert result['provider'] == 'lmstudio' and result['provider_name'] == 'LM Studio'
    assert result['model'] == 'local-chat' and result['ms'] >= 0
    assert asked == [('http://localhost:1234/v1', 'local-chat')]


def test_the_check_names_both_local_servers_when_nothing_is_running():
    def ask(provider, model):
        raise ConnectionError('connection refused')

    result = check(config(), ask=ask)

    assert result['ok'] is False and result['reason'] == 'unreachable'
    assert 'LM Studio' in result['hint'] and 'Ollama' in result['hint']
    assert 'localhost:1234' in result['hint']


def test_the_check_says_which_model_a_running_provider_does_not_serve():
    result = check(config(), ask=lambda provider, model: (_ for _ in ()).throw(Refused(404, 'model not found')))

    assert result['reason'] == 'missing_model' and 'local-chat' in result['hint']
    assert 'model not found' in result['detail']


def test_the_check_reads_a_model_that_could_not_be_loaded_as_a_missing_model():
    result = check(config(), ask=lambda provider, model: (_ for _ in ()).throw(
        Refused(400, 'Failed to load model "local-chat": no such file')))

    assert result['reason'] == 'missing_model' and 'local-chat' in result['hint']


def test_the_check_says_when_the_key_is_refused():
    result = check(config(), ask=lambda provider, model: (_ for _ in ()).throw(Refused(401, 'bad key')))

    assert result['reason'] == 'credentials' and 'API key' in result['hint']


def test_the_check_reports_a_model_that_answered_with_nothing():
    result = check(config(), ask=lambda provider, model: '   ')

    assert result['ok'] is False and result['reason'] == 'empty'


def test_the_check_refuses_a_provider_without_a_model():
    result = check(config(model='', provider='custom', custom={'base_url': 'http://localhost:9999/v1', 'default_model': ''}))

    assert result['reason'] == 'no_model' and result['model'] == ''
    assert result['ok'] is False


@pytest.mark.parametrize('provider', ['lmstudio', 'ollama', 'vllm'])
def test_a_fresh_local_provider_asks_for_a_model_instead_of_naming_one(provider):
    """A local server serves what the person loaded, so the starter config must not ask it for some other model."""
    result = check(config(model='', provider=provider), ask=lambda *_: pytest.fail('nothing to ask without a model'))

    assert result['reason'] == 'no_model' and result['model'] == ''


def test_the_cloud_presets_name_a_default_model():
    from core.providers import PROVIDER_PRESETS
    assert all(PROVIDER_PRESETS[key].default_model for key in ('openai', 'openrouter'))


def test_the_check_refuses_a_provider_that_is_switched_off():
    result = check(config(lmstudio={'enabled': False}))

    assert result['reason'] == 'no_provider'


def test_a_cloud_provider_is_told_to_check_its_key_not_to_start_lm_studio():
    result = check(config(provider='openai'), ask=lambda provider, model: (_ for _ in ()).throw(ConnectionError('down')))

    assert 'LM Studio' not in result['hint'] and 'API key' not in result['hint']
    assert 'openai' in result['hint'].lower() or 'address' in result['hint'].lower()


# ---------------------------------------------------------------- the route

@pytest.fixture
def client(world):
    return create_app('pages', 'setup', 'core_api').test_client()


def test_the_setup_route_answers_with_the_check_result(client, monkeypatch):
    monkeypatch.setattr(setup_routes, 'check_model', lambda: {'ok': False, 'reason': 'unreachable', 'hint': 'start a server'})

    response = client.post('/api/setup/check')

    assert response.status_code == 200 and response.headers['Cache-Control'] == 'no-store'
    assert response.get_json() == {'ok': False, 'reason': 'unreachable', 'hint': 'start a server'}


def test_the_check_never_reaches_a_real_model(monkeypatch):
    # The route is the only caller; if it ever stopped passing a fake, a test would make a real inference call.
    monkeypatch.setattr(setup, '_ask', lambda provider, model: pytest.fail('the setup check called a real model'))
    result = check(config(), ask=lambda provider, model: 'OK')
    assert result['ok'] is True


# ---------------------------------------------------------------- the first run

def test_a_first_run_is_sent_to_setup_and_the_page_asks_only_for_a_name(client, world):
    assert client.get('/api/onboarding/status').get_json()['complete'] is False

    response = client.get('/home')
    assert response.status_code == 302 and response.headers['Location'].endswith('/setup')

    page = client.get('/setup')
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert 'id="setupName"' in html and 'id="setupCheck"' in html and 'id="setupOptional"' in html
    assert 'id="onboardingName"' not in html


def test_the_home_page_returns_once_the_first_run_is_finished(client, world):
    from core.profile import complete_onboarding
    complete_onboarding()
    assert client.get('/home').status_code == 200


def test_finishing_setup_keeps_the_name_and_stops_asking(client, world):
    response = client.post('/api/onboarding/complete', json={'name': '  Sam  '})

    assert response.status_code == 200
    assert client.get('/api/onboarding/status').get_json() == {'complete': True, 'name': 'Sam'}
    assert client.get('/home').status_code == 200


def test_skipping_setup_is_remembered_and_saves_no_name(client, world):
    client.post('/api/onboarding/complete', json={})

    assert client.get('/api/onboarding/status').get_json() == {'complete': True, 'name': ''}
    assert client.get('/home').status_code == 200


def test_a_blank_name_keeps_the_one_already_saved(client, world):
    from core.profile import load_user_profile, save_user_profile
    profile = load_user_profile()
    profile['name'] = 'Robin'
    save_user_profile(profile)

    client.post('/api/onboarding/complete', json={'name': ''})

    assert client.get('/api/onboarding/status').get_json()['name'] == 'Robin'
    assert load_user_profile()['name'] == 'Robin'

def test_a_reasoning_model_that_only_thought_still_counts_as_answering():
    from types import SimpleNamespace

    from core import setup
    assert setup.PROBE_MAX_TOKENS >= 1024           # room to think before saying OK
    assert setup._thought(SimpleNamespace(content='', model_extra={'reasoning_content': 'The user wants OK.'}))
    assert setup._thought(SimpleNamespace(content='', reasoning='Thinking.', model_extra={}))
    assert not setup._thought(SimpleNamespace(content='', model_extra={}))
