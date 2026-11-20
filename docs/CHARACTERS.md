# Characters

A **character** is someone from a character card: the PNG (or `.json`) files that character-chat apps share, with the
character written inside. Altera reads Character Card V2 (`chara_card_v2`), and V1 and V3 cards too. A character uses the
same chat page, speech and controls as everyone else.

## Importing a card

On **Home** (`/home`), choose **Import a character card** (on the Characters tile's empty note, or **Import a card**
beside the Characters gallery) and pick the file. The chat opens with the card's first message.

- **PNG cards** carry the card in a text chunk (`chara` for V1/V2, `ccv3` for V3, `tavernCardV2` is also read; `tEXt`,
  `zTXt` and `iTXt` chunks all work). When a PNG has more than one, the V3 card wins. The picture becomes the
  character's portrait, with the card text stripped out of it.
- **JSON cards** are the card on its own. The portrait is the character's initial on a colour chosen from its id.
- Either kind can get a portrait from NPC Forge later, redrawn from the card's picture or painted from a description,
  in the character's settings on Home: see [Portraits](PORTRAITS.md). The card's own picture is kept to go back to.
- A card is refused, with the reason, when it is not a PNG or JSON card, has no name, has none of a description,
  personality or first message, is damaged or too
  large (20 MB file, 2 MB card, 8192 px a side), breaks the same hard content rules as every avatar card (every text
  field is checked, the creator's notes included), or its picture looks under age to the age check.

Nothing is read from the card file again after import: the file is saved exactly as it came in
`data/characters/<id>.png` (or `.json`) so you keep the original, and `data/avatars/<id>/avatar.json` (`"kind":
"character"`) is what Altera reads. Because both live under `data/`, backups and restore cover them; **Remove** deletes
both.

## How a card is used

| Card field | In Altera |
|---|---|
| `description`, `personality`, `scenario` | the system prompt: who they are, their personality and the situation |
| `first_mes` | the chat's first message, exactly as written (no model call). **Regenerate** on it picks from `alternate_greetings` |
| `mes_example` | example dialogue in the system prompt, cut to a token budget: whole `<START>` blocks first, then by line |
| `system_prompt` | put first in the system prompt (`{{original}}` is removed) while the card's instructions are on |
| `post_history_instructions` | added after your newest message while the card's instructions are on |
| `creator_notes` | shown in the character's settings on Home only, as plain text; never sent to a model |
| `character_book` | the lorebook: entries called up into the system prompt by the recent messages (see below) while the lorebook is on |

`{{char}}` / `<BOT>` become the character's name and `{{user}}` / `<USER>` your name from the profile (or "User").

The example budget is 600 tokens; set `characters.example_tokens` in `config.yaml` to change it (0 turns examples off,
4000 at most).

## The lorebook

A card's `character_book` (its lorebook, or world info) is a list of entries, each with keys and some text. Before each
reply Altera looks through the last few messages, yours and the character's, the new one included:

- An entry comes in when one of its **keys** is in them, as a whole word or phrase (`Kestrel` matches "the Kestrel's
  hull", not "Kestrels"). Keys ignore case unless the entry is `case_sensitive`.
- A **selective** entry needs one of its `secondary_keys` as well.
- An entry marked **constant** is always in.
- An entry switched off in the card (`enabled: false`) is never used.

The entries called up go into the system prompt in the card's `insertion_order`, in a section right after who the
character is, with `{{char}}` and `{{user}}` filled in. They are kept within a token budget: always-on entries are chosen
first, then the others in the card's order, and an entry that does not fit is skipped for a smaller one after it.

- **Budget**: 500 tokens, or the card's `token_budget` when that is smaller. Set `characters.book_tokens` in
  `config.yaml` to change it (0 turns lorebooks off, 4000 at most).
- **How far back**: 4 messages, or the card's `scan_depth` when that is smaller. Set `characters.book_scan_depth` to
  change it (50 at most).

Not used: keys written as regular expressions (`/pattern/` or V3's `use_regex`), because a badly written pattern can
hang the app; entries calling up other entries (`recursive_scanning`); and an entry's `position`, since every entry goes
in the same place. Separate lorebook files (SillyTavern World Info) cannot be imported yet.

Every entry, its keys and its name are checked against the same hard rules as the rest of the card at import, and a
book that breaks them stops the import. A character imported before Altera read lorebooks has its book read once from
the card file saved at import, the first time it is needed.

Altera's own rules and its safety checks on every message still apply whatever a card says. A card's instructions are
added to the prompt, they never replace Altera's.

## Settings

**Settings** under a character on Home changes:

- **Voice**, the same choices as for pals.
- **What it keeps**: memory, notes, story and check-ins, all on to start with (characters live in their story); switch
  any off and it works as described in [Pals](PALS.md#what-a-pal-keeps).
- **Use the card's own instructions**: on to start with. Off ignores the card's `system_prompt` and
  `post_history_instructions`. It is greyed out when the card has neither.
- **Use the card's lorebook**: on to start with, shown when the card has one, with its entries listed (names and keys,
  read only: what an entry says is not shown). Entries switched off in the card or using regular expressions are marked.

The name stays the card's. Characters have no photos.
