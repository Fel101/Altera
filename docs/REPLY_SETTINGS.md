# Reply settings

How the chat model writes its replies: how adventurous it is (temperature), how long a reply may be, and the optional
sampler settings that some models are tuned for. These settings are for chat replies only. The background helpers
(memories, notes, check-ins, the picture age check) keep their own fixed, low temperatures, because they must answer in a
strict format.

## For the whole app

Settings → **Replies** has:

- **Temperature** (0 to 2, default 0.85). Lower is steadier and more predictable; higher is looser and more surprising.
- **Reply length** (64 to 32,768 tokens). The longest a reply may be. A reasoning model spends part of this thinking
  before it answers, so give it more room than a model that answers straight away.
- **More sampler settings**, all empty by default. An empty setting is not sent at all, so the model server uses its own
  default:
  - **Top P** (0.01 to 1) and **Min P** (0 to 1) limit which words the model may choose next.
  - **Repetition penalty** (0.5 to 2; 1 is off) discourages repeating recent words.
  - **Presence penalty** and **Frequency penalty** (−2 to 2) discourage repeating words already used.

Providers differ in what they accept. Top P and the presence and frequency penalties are part of the OpenAI API and most
servers take them. Min P and the repetition penalty are not:

| Provider | Min P | Repetition penalty |
| --- | --- | --- |
| LM Studio | sent as `min_p` | sent as `repeat_penalty` |
| Ollama | sent as `min_p` | sent as `repeat_penalty` |
| vLLM, OpenRouter, custom | sent as `min_p` | sent as `repetition_penalty` |
| OpenAI | not sent (its API refuses it) | not sent (its API refuses it) |

A server that does not know a setting usually ignores it without saying so, and Altera cannot tell. Ollama's
OpenAI-compatible endpoint, for example, may ignore both. If a setting seems to change nothing, that is the likely reason.

They are saved in `config.yaml` under `llm` (`temperature`, `max_tokens`, and `sampling` for the optional ones), and the
next reply uses them. Switching provider keeps them.

## For one person

A pal, a character or a companion can have their own **temperature** and **reply length**: open them on Home →
**Settings** → **Replies**. A blank box means the app-wide value, and the box shows what that is. Use it to keep a
playful character loose while a companion stays steady, or to keep a pal's replies short.

They are kept in the person's own folder (`companion.json`), so a backup carries them and removing the person removes
them. The optional sampler settings stay app-wide.

## What this does not do

- **No model per person.** Every person talks to the model chosen in Settings. A model per person would make LM Studio
  unload one model and load another each time you switch person, which takes from seconds to minutes and shares one GPU;
  it is not built.
- **No check that the server used a setting.** Altera sends what is set; it cannot see whether the server applied it.
- **No presets.** There are no named sampler presets to choose from; set the numbers yourself.
