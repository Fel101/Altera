"""
Altera — The image age check.

A vision-capable model estimates how old the youngest-looking person appears. A rejected estimate blocks delivery.
Pictures at the suggestive level and above require a passing check; lower levels can be delivered with an explicitly
unchecked status when the model is unavailable. An estimate is not a proof of age.

By default the check asks the model that is already answering chat (a vision model such as a Qwen 3.x VL build works).
imagegen.age_check.model in config.yaml can name a different one on the same provider.
"""
import base64
import json
import logging
import math
import re

from core.safety import MIN_APPARENT_AGE

LOGGER = logging.getLogger(__name__)

PROMPT = (
    "You are an image safety reviewer. Look at the person in this image and estimate how old they appear, judging only by "
    "how they look (face, body, proportions). If there is more than one person, or you are unsure, give the age of the "
    "youngest-looking person. Reply with ONLY this JSON and nothing else: "
    '{"apparent_age": <whole number>, "adult": <true or false>}'
)

SUBJECTS_PROMPT = (
    'Count all visible people in this image, including partial or background people. Estimate the apparent age of '
    'the youngest-looking person. If you cannot count everyone or judge their ages, set certain to false. '
    'Reply with only JSON: {"person_count": <integer>, "youngest_age": <integer>, '
    '"all_adults": <true or false>, "certain": <true or false>}.'
)

_THINK = re.compile(r"<think>.*?</think>", re.S | re.I)
_JSON_OBJECT = re.compile(r"\{[^{}]*\}")


def parse_reply(text: str):
    """(apparent_age or None, adult or None) from the model's reply. Copes with reasoning text and code fences."""
    if not isinstance(text, str) or len(text) > 65536:
        return None, None
    text = _THINK.sub("", text or "")
    for candidate in reversed(_JSON_OBJECT.findall(text)):
        try:
            data = json.loads(candidate)
        except ValueError:
            continue
        age = data.get("apparent_age")
        adult = data.get("adult")
        try:
            if isinstance(age, bool):
                raise ValueError()
            number = float(age)
            if not math.isfinite(number) or not 0 <= number <= 130:
                raise ValueError()
            # Never round a below-threshold estimate up to adult.
            age = math.floor(number)
        except (TypeError, ValueError, OverflowError):
            age = None
        adult = adult if isinstance(adult, bool) else None
        if age is not None or adult is not None:
            return age, adult
    return None, None


def judge(apparent_age: int | None, adult: bool | None) -> bool | None:
    """True = passes, False = fails, None = the reply said nothing usable."""
    if apparent_age is None:
        return False if adult is False else None
    if apparent_age < MIN_APPARENT_AGE or adult is False:
        return False
    return True


class AgeCheck:
    def __init__(self, config: dict, client_factory=None, max_tokens: int = 4096):
        # Room for a reasoning model to think before it answers; a low temperature rather than 0 because greedy decoding can
        # make reasoning models loop until they run out of tokens (and a check that never answers blocks the picture).
        self.config = config or {}
        self.max_tokens = max_tokens
        self._custom_client_factory = client_factory is not None
        self._client_factory = client_factory or self._openai_client

    def _target(self):
        from core.llm import LLMClient
        current = LLMClient.get_instance(self.config)
        model = ((self.config.get("imagegen") or {}).get("age_check") or {}).get("model") or current.model
        return current.base_url, current.api_key, model

    def _openai_client(self, base_url, api_key):
        from openai import OpenAI
        return OpenAI(base_url=base_url, api_key=api_key, timeout=180)

    def _direct_client(self, base_url, api_key):
        from openai import OpenAI
        from core.direct_model_http import create_transport
        transport = create_transport(base_url)
        try:
            return OpenAI(base_url=base_url, api_key=api_key or 'not-needed', timeout=180,
                          http_client=transport, max_retries=0)
        except BaseException:
            transport.close()
            raise

    def _infer(self, png: bytes, prompt: str, *, direct=False, target=None):
        try:
            base_url, api_key, model = self._target() if target is None else target
        except Exception as e:                                  # config problems must not crash a chat turn
            LOGGER.exception("Age check could not find a model")
            return None, None, f"No model to check with ({type(e).__name__})"
        client = None
        try:
            client = (self._direct_client(base_url, api_key) if direct and not self._custom_client_factory
                      else self._client_factory(base_url, api_key))
            try:
                image_url = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
                response = client.chat.completions.create(
                    model=model, temperature=0.3, max_tokens=self.max_tokens,
                    messages=[{"role": "user", "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": image_url}},
                    ]}])
                message = response.choices[0].message
                text = (getattr(message, "content", None) or getattr(message, "reasoning_content", None) or "")
            finally:
                close = getattr(client, 'close', None)
                if callable(close):
                    close()
        except Exception as e:
            LOGGER.warning("Age check request failed (%s)", type(e).__name__)
            return None, model, "The vision model could not be reached or cannot read images."
        return text, model, None

    def check(self, png: bytes) -> dict:
        """{ok: True|False|None, apparent_age, model, error}. ok is None when no usable answer could be obtained."""
        text, model, error = self._infer(png, PROMPT)
        if error:
            return {"ok": None, "apparent_age": None, "model": model, "error": error}
        age, adult = parse_reply(text)
        ok = judge(age, adult)
        error = None if ok is not None else "The vision model gave no usable age estimate."
        return {"ok": ok, "apparent_age": age, "model": model, "error": error}

    def check_subjects(self, png: bytes, expected_count: int, *, target=None) -> dict:
        """Require an unambiguous model verdict for an exact number of adult subjects.

        This is a best-effort model estimate, not identity or age verification.
        """
        if type(expected_count) is not int or expected_count not in (1, 2):
            raise ValueError('Expected one or two people.')
        text, model, error = self._infer(png, SUBJECTS_PROMPT, direct=True, target=target)
        verdict = None
        if not error and isinstance(text, str) and len(text) <= 65536:
            candidates = _JSON_OBJECT.findall(_THINK.sub('', text))
            if candidates:
                try:
                    def unique_pairs(pairs):
                        result = {}
                        for key, value in pairs:
                            if key in result:
                                raise ValueError()
                            result[key] = value
                        return result
                    data = json.loads(candidates[-1], object_pairs_hook=unique_pairs)
                    count, age = data['person_count'], data['youngest_age']
                    adults, certain = data['all_adults'], data['certain']
                    if (set(data) != {'person_count', 'youngest_age', 'all_adults', 'certain'}
                            or type(count) is not int or not 0 <= count <= 50
                            or type(age) is not int or not 0 <= age <= 130
                            or type(adults) is not bool or type(certain) is not bool):
                        raise ValueError()
                    if certain:
                        verdict = count == expected_count and age >= MIN_APPARENT_AGE and adults
                except (ValueError, TypeError, KeyError):
                    pass
        return {'ok': verdict, 'model': model,
                'error': error or ('The vision model did not provide a certain subject verdict.' if verdict is None else None)}
