"""Listing a provider's models: a model server that is not running is ordinary, so it is one log line, never a traceback."""
import logging

import pytest
import requests

from core import providers
from test_blueprints import create_app


class Reply:
    def __init__(self, status=200, body=None):
        self.status_code, self.body = status, body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(response=self)

    def json(self):
        if self.body is None:
            raise ValueError('not JSON')
        return self.body


@pytest.mark.parametrize(('outcome', 'reason'), [
    (requests.ConnectionError('refused'), 'nothing is answering there'),
    (requests.Timeout('slow'), 'it did not answer in time'),
    (Reply(503), 'it answered HTTP 503'),
    (Reply(200, None), 'not an OpenAI-style model list'),
    (Reply(200, ['a', 'list']), 'not an OpenAI-style model list'),
])
def test_a_provider_that_cannot_list_its_models_is_one_warning_and_an_empty_list(monkeypatch, caplog, outcome, reason):
    def get(url, **kwargs):
        if isinstance(outcome, Exception):
            raise outcome
        return outcome
    monkeypatch.setattr(providers.requests, 'get', get)
    provider = providers.ProviderConfig(name='LM Studio', base_url='http://127.0.0.1:9/v1')
    with caplog.at_level(logging.WARNING, logger='core.providers'):
        assert providers.fetch_models_from_provider(provider) == []
    [record] = caplog.records
    assert record.levelno == logging.WARNING and record.exc_info is None
    assert reason in record.getMessage() and 'http://127.0.0.1:9/v1' in record.getMessage()


def test_the_settings_route_lists_models_through_the_same_helper(monkeypatch):
    seen = []
    monkeypatch.setattr('core.routes.llm.fetch_models_from_provider',
                        lambda provider: seen.append(provider.name) or [{'id': 'tiny', 'object': 'model', 'owned_by': ''}])
    response = create_app('llm').test_client().get('/api/llm/providers/lmstudio/models')
    assert response.status_code == 200
    assert response.get_json() == {'provider': 'lmstudio', 'models': [{'id': 'tiny', 'object': 'model', 'owned_by': ''}]}
    assert len(seen) == 1
