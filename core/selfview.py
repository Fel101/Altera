"""
Altera — How she sees herself.

The first time a new companion is met, the vision model looks at her portrait once and describes what it sees in her own
words ("I have dark eyes and a crooked nose..."). That text becomes part of who she is (core/prompts.py), so when she meets
you she has seen her own face, and it is her own account, not a generated caption of somebody else.

It uses the same vision model as the age check (imagegen.age_check.model, or the chat model). If no vision model answers, she
simply goes without: her look is still known from the character card.
"""
import base64
import logging
import re
from datetime import datetime

from core import safety

LOGGER = logging.getLogger(__name__)

MAX_CHARS = 700
_THINK = re.compile(r"<think>.*?</think>", re.S | re.I)

PROMPT = (
    "This is a picture of YOU: the character you play, {name}. Look at it as if you were seeing your own face in a mirror for "
    "the very first time. In three or four plain sentences, in the first person, say what you see: your face, eyes, hair, "
    "expression, style, and the impression you give. Do not name any real person, do not say how old you are, and do not "
    "mention a picture, an image or a photo. Reply with only those sentences."
)


def clean(text: str) -> str:
    """One tidy paragraph from the model's reply (reasoning text, quotes and line breaks removed)."""
    text = _THINK.sub("", text or "")
    text = re.sub(r"\s+", " ", text).strip().strip("\"'`")
    return text[:MAX_CHARS].rsplit(" ", 1)[0].strip() if len(text) > MAX_CHARS else text


def describe(png: bytes, name: str, config: dict, client_factory=None) -> str:
    """Her account of her own face, or '' if no vision model answered or the answer broke a rule."""
    try:
        from core.age_check import AgeCheck
        checker = AgeCheck(config, client_factory=client_factory)
        base_url, api_key, model = checker._target()
        client = checker._client_factory(base_url, api_key)
        try:
            image_url = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
            response = client.chat.completions.create(
                model=model, temperature=0.6, max_tokens=2048,
                messages=[{"role": "user", "content": [
                    {"type": "text", "text": PROMPT.format(name=name)},
                    {"type": "image_url", "image_url": {"url": image_url}},
                ]}])
            message = response.choices[0].message
            text = clean(getattr(message, "content", None) or "")
        finally:
            close = getattr(client, 'close', None)
            if callable(close):
                close()
    except Exception as e:
        LOGGER.warning("Could not get her view of herself (%s)", type(e).__name__)
        return ""
    if not text or safety.hard_problem(text):
        return ""
    return text


def ensure(avatars, avatar_id: str, config: dict, client_factory=None) -> str:
    """Her view of herself, made now if she has none yet. Returns it ('' if there is none)."""
    state = avatars.state(avatar_id)
    if state.get("self_view"):
        return state["self_view"]
    if state.get("self_view_tried"):                       # it failed once; do not keep asking the model on every meeting
        return ""
    record = avatars.get(avatar_id)
    portrait = avatars.image_path(avatar_id, "portrait")
    if not record or not portrait:
        return ""
    text = describe(portrait.read_bytes(), record.get("name") or "you", config, client_factory)
    avatars.update_state(avatar_id, self_view=text, self_view_tried=True,
                         self_view_at=datetime.now().isoformat(timespec="seconds"))
    return text
