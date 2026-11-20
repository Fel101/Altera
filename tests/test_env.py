"""ALTERA_* environment variables."""
from core import config as config_module
from core import env


def test_a_setting_is_read_from_its_altera_name_only():
    assert env.get('HOME', environ={'ALTERA_HOME': 'new'}) == 'new'
    assert env.get('HOME', 'default', environ={'ALTERA_HOME': ''}) == 'default'
    assert env.get('HOME', 'default', environ={}) == 'default'


def test_provider_keys_are_recognised():
    assert env.is_api_key('ALTERA_OPENROUTER_API_KEY')
    assert not env.is_api_key('ALTERA_HOME') and not env.is_api_key('OTHER_OPENROUTER_API_KEY')


def test_a_provider_key_from_the_environment_reaches_the_config(monkeypatch):
    monkeypatch.setenv('ALTERA_SYNTHETIC_API_KEY', 'new-key')
    assert config_module._apply_key_env({'synthetic': {'api_key': ''}})['synthetic']['api_key'] == 'new-key'
