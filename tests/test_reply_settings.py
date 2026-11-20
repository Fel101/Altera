"""Reply settings: temperature, reply length and the optional sampler settings, app-wide and per person.

The model is a fake OpenAI client that records what each request asked for; nothing reaches a real model."""
from types import SimpleNamespace as NS

import pytest
import yaml

from core import companions, pals, sampling
from core.chat import ChatEngine
from core.llm import LLMClient
from test_blueprints import create_app
from test_companion_api import FakeMemory, tmp_store, world  # noqa: F401


@pytest.fixture
def requests_made(monkeypatch):
    """Every chat.completions.create call, as the keyword arguments it got."""
    made = []

    def create(**kwargs):
        made.append(kwargs)
        return NS(choices=[NS(message=NS(content="Hello there.", tool_calls=None))], usage=None)

    monkeypatch.setattr("openai.OpenAI", lambda **kwargs: NS(chat=NS(completions=NS(create=create)), close=lambda: None))
    return made


def make_pal(world, name="Pip"):
    return pals.create(world.avatars, name, "sunny", "", engine="kokoro")


# ---------------------------------------------------------------- checking values

@pytest.mark.parametrize("key, value, message", [
    ("temperature", 2.5, "Temperature must be from 0 to 2."),
    ("temperature", "warm", "Temperature must be a number."),
    ("temperature", True, "Temperature must be a number."),
    ("temperature", float("nan"), "Temperature must be a number."),
    ("max_tokens", 10, "Reply length must be from 64 to 32768."),
    ("max_tokens", 300.5, "Reply length must be a whole number."),
    ("top_p", 0, "Top P must be from 0.01 to 1."),
    ("presence_penalty", -3, "Presence penalty must be from -2 to 2."),
])
def test_a_value_outside_its_limits_is_refused_with_a_plain_message_and_nothing_changes(key, value, message):
    llm = {"temperature": 0.85, "max_tokens": 1024}
    with pytest.raises(sampling.SamplingError, match=message.replace(".", r"\.")):
        sampling.apply_app_changes(llm, {key: value})
    assert llm == {"temperature": 0.85, "max_tokens": 1024}


def test_an_optional_setting_sent_as_null_is_cleared_and_unknown_fields_are_refused():
    llm = sampling.apply_app_changes({"temperature": 0.85}, {"top_p": 0.9, "min_p": "0.05"})
    assert llm["sampling"] == {"top_p": 0.9, "min_p": 0.05}
    llm = sampling.apply_app_changes(llm, {"top_p": None})
    assert llm["sampling"] == {"min_p": 0.05}
    with pytest.raises(sampling.SamplingError):
        sampling.apply_app_changes(llm, {"top_k": 40})
    with pytest.raises(sampling.SamplingError, match="must be a number"):
        sampling.apply_app_changes(llm, {"temperature": None})          # these two always apply


def test_a_hand_edited_value_that_is_no_longer_valid_is_ignored_rather_than_sent():
    shown = sampling.app_settings({"llm": {"temperature": "hot", "max_tokens": 5,
                                           "sampling": {"top_p": 7, "min_p": 0.1, "unknown": 1}}})
    assert shown == {"temperature": sampling.TEMPERATURE_DEFAULT, "max_tokens": sampling.MAX_TOKENS_DEFAULT,
                     "sampling": {"min_p": 0.1}}


# ---------------------------------------------------------------- what each provider is sent

def test_only_the_settings_that_are_set_are_sent_and_each_provider_gets_its_own_name_for_repetition():
    chosen = {"top_p": 0.9, "min_p": 0.05, "repetition_penalty": 1.1, "presence_penalty": None}
    assert sampling.request_options("lmstudio", chosen) == ({"top_p": 0.9}, {"min_p": 0.05, "repeat_penalty": 1.1})
    assert sampling.request_options("ollama", chosen) == ({"top_p": 0.9}, {"min_p": 0.05, "repeat_penalty": 1.1})
    assert sampling.request_options("openrouter", chosen) == ({"top_p": 0.9}, {"min_p": 0.05, "repetition_penalty": 1.1})
    assert sampling.request_options("custom", {}) == ({}, {})


def test_openais_own_api_is_never_sent_the_settings_it_refuses():
    kwargs, extra = sampling.request_options("openai", {"top_p": 0.9, "min_p": 0.05, "repetition_penalty": 1.1,
                                                        "frequency_penalty": 0.4})
    assert kwargs == {"top_p": 0.9, "frequency_penalty": 0.4} and extra == {}
    assert sampling.not_sent("openai") == ["min_p", "repetition_penalty"] and sampling.not_sent("lmstudio") == []


def test_the_client_sends_set_settings_in_both_the_plain_and_the_streamed_request(requests_made):
    client = LLMClient(base_url="http://127.0.0.1:9/v1", model="m", provider_key="lmstudio",
                       sampling={"top_p": 0.9, "repetition_penalty": 1.1})
    client.chat([{"role": "user", "content": "hi"}])
    list(LLMClient(base_url="http://127.0.0.1:9/v1", model="m", provider_key="lmstudio")
         .chat([{"role": "user", "content": "hi"}]))
    plain, without = requests_made
    assert plain["top_p"] == 0.9 and plain["extra_body"] == {"repeat_penalty": 1.1}
    assert "top_p" not in without and "extra_body" not in without                   # nothing set: nothing sent

    def stream(**kwargs):
        requests_made.append(kwargs)
        return iter([])

    fake = NS(chat=NS(completions=NS(create=stream)))
    list(client._chat_stream(fake, [{"role": "user", "content": "hi"}]))
    assert requests_made[-1]["top_p"] == 0.9 and requests_made[-1]["extra_body"] == {"repeat_penalty": 1.1}


def test_background_helpers_never_get_the_sampler_settings(monkeypatch):
    import core.llm as llm_module
    monkeypatch.setattr(LLMClient, "_instance", LLMClient(model="m", provider_key="lmstudio", sampling={"top_p": 0.5}))
    assert llm_module.get_client().sampling == {}


def test_the_sampler_settings_survive_a_provider_switch():
    from core.providers import ProviderRegistry
    registry = ProviderRegistry({"llm": {"provider": "lmstudio", "sampling": {"min_p": 0.05}}})
    assert registry.to_config_dict()["sampling"] == {"min_p": 0.05}


# ---------------------------------------------------------------- a person's own

def test_a_persons_own_temperature_and_length_are_kept_in_their_folder_and_null_goes_back_to_the_app(world):
    made = make_pal(world)
    assert sampling.person_settings(world.avatars, made["id"]) == {"temperature": None, "max_tokens": None}
    assert sampling.set_person(world.avatars, made["id"], {"temperature": 1.2, "max_tokens": 300}) == \
        {"temperature": 1.2, "max_tokens": 300}
    assert world.avatars.state(made["id"])["replies"] == {"temperature": 1.2, "max_tokens": 300}
    assert sampling.set_person(world.avatars, made["id"], {"temperature": None}) == {"temperature": None, "max_tokens": 300}
    with pytest.raises(sampling.SamplingError):
        sampling.set_person(world.avatars, made["id"], {"top_p": 0.9})               # the samplers stay app-wide
    with pytest.raises(sampling.SamplingError) as missing:
        sampling.set_person(world.avatars, "nobody-1", {"temperature": 1})
    assert missing.value.status == 404


def test_a_pal_edit_from_pal_yaml_keeps_its_reply_settings(world):
    made = make_pal(world)
    sampling.set_person(world.avatars, made["id"], {"max_tokens": 300})
    pals.update(world.avatars, made["id"], name="Pippa")
    assert sampling.person_settings(world.avatars, made["id"])["max_tokens"] == 300


@pytest.fixture
def real_engine(world, monkeypatch, altera_config, requests_made):
    import core.memory
    from core.config import get_personality
    monkeypatch.setattr(core.memory, "memory_manager", FakeMemory(), raising=False)
    config = yaml.safe_load(altera_config.read_text(encoding="utf-8"))
    eng = ChatEngine(config, get_personality())
    eng._supports_tools = False
    shared = LLMClient(base_url="http://127.0.0.1:9/v1", model="m", temperature=0.85, max_tokens=1024,
                       provider_key="lmstudio", sampling={"min_p": 0.05})
    monkeypatch.setattr(ChatEngine, "llm_client", property(lambda self: shared))
    return NS(engine=eng, shared=shared)


def test_a_reply_uses_the_persons_own_temperature_and_length_over_the_apps(world, real_engine, requests_made):
    loose, plain = make_pal(world, "Loose"), make_pal(world, "Plain")
    sampling.set_person(world.avatars, loose["id"], {"temperature": 1.4, "max_tokens": 300})
    for person in (loose, plain):
        sid = companions.open_companion(person["id"])["session_id"]
        real_engine.engine.send_message("Hello", sid)
    own, app = requests_made
    assert (own["temperature"], own["max_tokens"]) == (1.4, 300)
    assert (app["temperature"], app["max_tokens"]) == (0.85, 1024)
    assert own["extra_body"] == app["extra_body"] == {"min_p": 0.05}                 # the samplers are app-wide
    assert (real_engine.shared.temperature, real_engine.shared.max_tokens) == (0.85, 1024)   # the shared one is untouched


# ---------------------------------------------------------------- the routes

@pytest.fixture
def client(world, altera_config):
    return create_app("companions", "llm").test_client()


def test_settings_reads_and_saves_the_app_wide_reply_settings(client, altera_config):
    shown = client.get("/api/llm/sampling").get_json()
    assert set(shown) >= {"temperature", "max_tokens", "sampling", "provider", "not_sent", "limits"}
    saved = client.put("/api/llm/sampling", json={"temperature": 0.7, "top_p": 0.9, "min_p": None})
    assert saved.status_code == 200
    assert saved.get_json()["temperature"] == 0.7 and saved.get_json()["sampling"] == {"top_p": 0.9}
    written = yaml.safe_load(altera_config.read_text(encoding="utf-8"))["llm"]
    assert written["temperature"] == 0.7 and written["sampling"] == {"top_p": 0.9}
    assert LLMClient.get_instance().sampling == {"top_p": 0.9}                       # the next reply uses it
    refused = client.put("/api/llm/sampling", json={"top_p": 3})
    assert refused.status_code == 400 and "Top P" in refused.get_json()["error"]
    assert client.put("/api/llm/sampling", json={"seed": 1}).status_code == 400


def test_the_person_routes_read_and_change_their_own_reply_settings_for_anyone(client, world):
    made = make_pal(world)
    url = f"/api/people/{made['id']}/replies"
    shown = client.get(url).get_json()
    assert shown["own"] == {"temperature": None, "max_tokens": None} and set(shown["app"]) == {"temperature", "max_tokens"}
    changed = client.put(url, json={"temperature": 1.1}).get_json()
    assert changed["own"] == {"temperature": 1.1, "max_tokens": None}
    assert client.put(url, json={"temperature": 9}).status_code == 400
    assert client.put(url, json={"temperature": 1}, headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert client.get("/api/people/nobody-here-1/replies").status_code == 404


def test_a_companion_from_npc_forge_gets_settings_instead_of_not_found(client, world):
    forged = world.avatars.import_from_forge("mara-bell-47ab97")
    shown = client.get(f"/api/companions/{forged['id']}")
    assert shown.status_code == 200
    assert shown.get_json()["written"] is False and shown.get_json()["name"]
    assert client.put(f"/api/companions/{forged['id']}", json={"name": "X"}).status_code == 404   # changed in NPC Forge
    made = make_pal(world)
    assert client.get(f"/api/companions/{made['id']}").status_code == 404
