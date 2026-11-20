"""Shared pictures send reference checks only to their reviewed local fixture."""
import json

import openai
import pytest
from openai import OpenAI as RealOpenAI

from core import age_check, direct_model_http

pytest_plugins = ('test_voice_http',)
_DIRECT_CLIENT = age_check.AgeCheck._direct_client


@pytest.mark.parametrize('status', [200, 307])
def test_shared_picture_vision_ignores_proxy_and_refuses_redirect(recipients, monkeypatch, status):
    elsewhere = recipients()
    body = json.dumps({'choices': [{'message': {'content': json.dumps({
        'person_count': 1, 'youngest_age': 30, 'all_adults': True, 'certain': True,
    })}}]}).encode()
    target = recipients(status=status, location=elsewhere.url if status != 200 else None, body=body)
    for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy'):
        monkeypatch.setenv(key, elsewhere.url)
    for key in ('NO_PROXY', 'no_proxy'):
        monkeypatch.delenv(key, raising=False)

    opened = []
    factory = direct_model_http.create_transport

    def keep_transport(url):
        assert url == target.url + '/v1'
        opened.append(factory(url))
        return opened[-1]

    monkeypatch.setattr(direct_model_http, 'create_transport', keep_transport)
    monkeypatch.setattr(openai, 'OpenAI', RealOpenAI)
    monkeypatch.setattr(age_check.AgeCheck, '_direct_client', _DIRECT_CLIENT)
    result = age_check.AgeCheck({}).check_subjects(b'synthetic image', 1, target=(target.url + '/v1', '', 'fixture'))

    assert result['ok'] is (True if status == 200 else None)
    assert len(target.received) == 1 and not elsewhere.received
    assert len(opened) == 1 and opened[0].is_closed
