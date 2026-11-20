"""
Altera — The [[photo: ...]] tag.

Altera sends pictures by putting a tag such as [[photo: sitting on a balcony at sunset with a coffee]] in her reply. The app
strips the tag from the text, turns it into a picture job, and shows the picture under her message. A tag works with any
chat model, including ones whose tool-calling is unreliable.

TagFilter removes tags from a streamed reply chunk by chunk (a tag can arrive split across chunks). photo_intent() and
scene_from_request() are the fallback for a model that ignores the instruction: if the user plainly asked for a picture,
one is made from their request.
"""
import re

MAX_TAG_LEN = 400
MAX_SCENE_LEN = 300

_START = re.compile(r"\s*photo\s*(?=[:\]])", re.I)                               # "[[photo:" or "[[photo]]"
_PARTIAL = re.compile(r"\s*(?:p(?:h(?:o(?:t(?:o\s*)?)?)?)?)?\Z", re.I)           # "[[", "[[ph", "[[photo " ...
_BODY_PREFIX = re.compile(r"^\s*photo\s*:?\s*", re.I)


def _clean_body(body: str) -> str:
    text = _BODY_PREFIX.sub("", body)
    text = re.sub(r"\s+", " ", text).strip().strip("\"'`")
    return text[:MAX_SCENE_LEN].strip()


class TagFilter:
    """Feed reply chunks in; get back the same text without [[photo: ...]] tags. Found tags are collected in .tags."""

    def __init__(self):
        self._held = ""
        self.tags: list[str] = []

    def feed(self, chunk: str) -> str:
        text = self._held + (chunk or "")
        self._held = ""
        out, pos = [], 0
        while True:
            i = text.find("[[", pos)
            if i < 0:
                if text.endswith("["):                      # might be the first half of "[["
                    out.append(text[pos:-1])
                    self._held = "["
                else:
                    out.append(text[pos:])
                break
            out.append(text[pos:i])
            rest = text[i + 2:]
            if not _START.match(rest):
                if _PARTIAL.match(rest):                    # could still become a tag: wait for more text
                    self._held = text[i:]
                    break
                out.append("[[")                            # ordinary text
                pos = i + 2
                continue
            j = text.find("]]", i + 2)
            if j < 0:
                if len(text) - i > MAX_TAG_LEN:             # far too long to be a tag
                    out.append("[[")
                    pos = i + 2
                    continue
                self._held = text[i:]
                break
            self.tags.append(_clean_body(text[i + 2:j]))
            pos = j + 2
        return "".join(out)

    def flush(self) -> str:
        """End of the reply. A tag that never closed is dropped; anything else held back is ordinary text."""
        held, self._held = self._held, ""
        if held.startswith("[[") and _START.match(held[2:]):
            return ""
        return held


def extract(text: str):
    """(text without tags, [tag descriptions]) for a complete reply."""
    f = TagFilter()
    clean = f.feed(text) + f.flush()
    return tidy(clean), f.tags


def tidy(text: str) -> str:
    """Remove the gaps a stripped tag leaves behind."""
    text = re.sub(r"[ \t]+\n", "\n", text or "")
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ---- fallback: the user plainly asked for a picture --------------------------------------------------------------------

_NOUN = r"(?:selfie|picture|pic|photo|snapshot|image)s?"           # "picture" before "pic": alternation takes the first match
_REQUEST = re.compile("|".join([
    rf"\b(?:send|share|text|show|snap|give)\s+me\b[^.?!\n]{{0,40}}?\b{_NOUN}\b",
    rf"\b(?:can|could|would|will)\s+(?:you|u)\s+(?:please\s+)?(?:take|send|share|show|snap)\b[^.?!\n]{{0,40}}?\b{_NOUN}\b",
    rf"(?<!let's )(?<!lets )(?<!let us )\btake\s+(?:a|an|another|one)\s+(?:\w+\s+){{0,2}}{_NOUN}\b",
    r"\b(?:selfie|pic|picture|photo)\s+of\s+(?:you|yourself|u)\b",
    r"\b(?:want|wanna|like|love|wish)\s+(?:to\s+)?see\s+(?:you|a\s+(?:pic|photo|picture)\s+of\s+you)\b",
    # a follow-up after she has already sent one, with no "send/show/take" verb: "another/one more/any other" plus the
    # word for it, or "can/could I see" (rather than "can/could you send") plus "another" or the word for it.
    rf"\b(?:another|one more|any (?:other|more))\b[^.?!\n]{{0,40}}?\b{_NOUN}\b",
    rf"\b(?:can|could)\s+i\s+see\b[^.?!\n]{{0,40}}?\b(?:another|{_NOUN})\b",
]), re.I)

_LEAD = re.compile(
    r"^\W*(?:(?:hey|hi|please|pls|plz|ok|okay|so|and|now|then|babe|baby|honey)\b[\s,!]*)*"
    r"(?:(?:can|could|would|will)\s+(?:you|u)\s+(?:please\s+)?)?"
    r"(?:send|share|text|show|snap|give|take|get)\s+(?:me\s+)?(?:a|an|another|one|some)?\s*"
    r"(?:(?:new|quick|cute|nice|little|fresh|good|casual)\s+)*"
    rf"{_NOUN}\s*(?:of\s+(?:you|yourself|u)\s*)?[,:;-]?\s*", re.I)

# A bare follow-up ("got any other pics?", "one more please") has no verb for _LEAD to strip and no scene detail either --
# it should reduce to nothing, same as "send me a selfie" does, so a default scene gets picked instead of the request's
# own wording.
_LEAD_FOLLOWUP = re.compile(
    r"^\W*(?:(?:hey|hi|please|pls|plz|ok|okay|so|and|now|then|babe|baby|honey|how about)\b[\s,!]*)*"
    rf"(?:(?:can|could)\s+i\s+see|got\s+any\s+(?:other|more)|any\s+(?:other|more)|one\s+more|another)\s*"
    rf"(?:{_NOUN})?\s*(?:of\s+(?:you|yourself|u)\s*)?[,:;-]?\s*", re.I)


def photo_intent(text: str) -> bool:
    """True if the user is asking her to send a picture."""
    return bool(_REQUEST.search(text or ""))


def scene_from_request(text: str) -> str | None:
    """What the user wants the picture to show ('in the kitchen'), or None to let a default scene be chosen."""
    scene = _LEAD.sub("", (text or "").strip(), count=1)
    scene = _LEAD_FOLLOWUP.sub("", scene, count=1)
    scene = re.sub(r"\b(?:yourself|your)\b", lambda m: "the" if m.group(0).lower() == "your" else "", scene, flags=re.I)
    scene = re.sub(r"\s+", " ", scene).strip(" ?!.,;:-")
    scene = re.sub(r"\s*\b(?:please|pls|plz)\b\s*$", "", scene, flags=re.I).strip(" ?!.,;:-")
    return scene[:MAX_SCENE_LEN] or None
