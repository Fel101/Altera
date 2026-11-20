# Our story and reflection

Open **Our story** from the chat sidebar and choose a companion. This reads saved data; it does not generate a greeting, play audio, open a conversation or run reflection. Choosing a companion here does not change the companion open in chat.


## Save a moment

Expand **Save an exchange or imagined scene**, choose one or two recent messages, and select **Review source text**. Give the moment a title and, optionally, your description and uncertainty. Choose its source label before saving:

| Label | Meaning |
| --- | --- |
| Actual exchange | Records words exchanged. A message describing a trip does not establish that the trip happened. |
| Imagined scene | Fiction or roleplay, kept separate from real shared history. |
| Derived candidate / your interpretation | A summary or interpretation that can be mistaken. |

Each source retains the message ID, original conversation, role, whole-message SHA-256 and a Unicode character range. The review uses at most the first 2,000 characters per message, 4,000 total. Source details show the exact current excerpts. A matching fingerprint establishes that the message is unchanged, not that the interpretation is true. An assistant's guess remains attributed to the assistant.

The picker lists up to 60 recent eligible messages from the selected saved conversation. Messages before a recall boundary, generated inbox text, and sources paused for repair are excluded. Nothing is saved until you choose **Save reviewed episode**. A lost acknowledgment keeps the same save identity: retry that save to retrieve its result before creating another moment.

**Our story** shows moments you explicitly include; **All episodes**, **Pinned** and **Needs review** offer other views. The page reads at most 200 recent retained episodes for the companion. A generated reflection candidate is excluded from episodic recall until you explicitly add it to Our story or correct its description. Pinning alone does not curate or confirm it.

## Priority and occasional recall

Use **Pin**, **Salience** or **Correct** on an episode. Pins prevent age decay; salience is your importance setting from 1 to 5. These controls do not change extraction confidence or make a stale source eligible. Confidence is a supplied score, not a measured probability that a claim is true.

Age lowers retrieval priority with a 30-day half-life and a 20% minimum factor. It never deletes a saved item. Episodic age uses the saved creation time. On **Memory → Retrieval priority**, vector memories also expose age, importance, pins and the number of completed reply contexts that included them. A successful inclusion refreshes that memory's known recency anchor; a failed reply or inspecting the report does not. Unknown legacy timestamps without an offset have neutral age decay until a known anchor exists.

Recall uses relevant source-backed episodes, at most two episodes, each with up to two source excerpts, within a 1,800-character input budget. The companion can receive a callback on at most two saved replies per UTC day and no more often than once in five completed reply turns. Reservations prevent concurrent conversations for the same companion from spending the same slot. Failed or interrupted replies release their reservation without spending it. A read-only recall preview never spends the budget.

If Altera stops after saving an answer but before recording a reserved callback, normal startup can recover that accounting against the original reply and source snapshot. Changed, forgotten or unavailable sources do not become eligible through recovery. An eligible expression cue and selected vector-memory recency can also recover from their original sources and use the reply's save time.

An eligible or pinned item is not guaranteed to fit a particular prompt or be mentioned by the model. The prompts ask Altera to retain attribution, keep imagined scenes fictional, preserve uncertainty, and ask when a personal detail is missing. Tests verify these instructions and data boundaries; actual model behavior still needs acceptance with the selected model.

## Optional reflection

Refresh keeps unfinished reflection edits. If the saved revision or provider changed, explicitly reload and review the current provider before saving. Save acknowledgments also keep any newer text typed while an episode or correction was being saved. Unavailable source messages keep their IDs for review but cannot be used for a new capture without current evidence.


Reflection starts **off for every companion**. Save an explicit opt-in in Our story before requesting a run or enabling unattended work. Presence/check-in approval is separate. An external provider additionally needs approval for the exact reflection connection, model and credential identity; a changed identity requires review. Altera does not switch to a fallback provider.

Defaults are a 1,440-minute interval, 1,440-minute cooldown, one attempt per local day, UTC, and quiet hours 22:00–08:00. Set an IANA timezone such as `America/New_York`. Quiet-hour boundaries follow timezone/DST rules. The daily cap can be 0–2; zero pauses work. The interval is a rolling interval, not a fixed nightly appointment. The server must remain running. The scheduler checks about every 30 seconds, catches up to a bounded recent occurrence, and expires missed work rather than replaying a backlog.


**Request one reflection** queues work under the same opt-in, quiet hours and frequency limits. **Refresh run reports** reads its outcome; the page does not poll or generate on opening. Each job makes at most one provider call with a 1,200-token output cap and a 15-second transport timeout. Failures count toward the attempt limits. No tools, pictures, speech or provider fallback are used.

The source window is the latest eight eligible original user messages within the latest 60 conversation rows, at most the first 1,000 Unicode characters of each. Saved dates accompany relative plans; a timestamp without an offset has an unknown timezone. Previous reflections, episodes, assistant statements, accepted check-ins and notebook summaries are never fresh evidence. The model can propose at most two episodes and two derived notebook entries, with exact source ranges, uncertainty and supplied confidence.

Valid results publish atomically to SQLite. **Current derived notebook** entries join the prompt as explicitly labelled interpretations; your existing editable notebook files remain separate. Correct or dismiss an entry from the report. Edits retain source provenance and a derived label. Stable original-evidence keys prevent another run from silently overwriting a correction or recreating a dismissed entry with new wording. Run reports retain the run’s source references and link to the current derived entries. A derived notebook entry is not an independent confirmation of a saved fact.

New ordinary notebook updates also use original user messages as fresh evidence. Earlier notebook text and manual edits remain derived material, with aggregate provenance limits; this update does not retroactively verify older notes. The [knowledge report](MEMORY_CONTROLS.md) includes source-tracked current reflection entries and their exact excerpts without asking a model to summarize them.

## Correct, Forget and restore

Wait for active replies before correcting derived notes or episodes. Editing/deleting/replacing a source message pauses derived context. Rebuild eligible automatic knowledge through [Memory controls](MEMORY_CONTROLS.md); stale episodes require new current evidence. A pin cannot bypass source or recall boundaries.

**Forget episode** clears that episode's saved text and source references, clears the companion's notebooks and their history, and establishes a fresh recall boundary. Earlier chat stays visible but is excluded from future recalled history. Pending reflection/check-ins and other affected derived entries are invalidated. A failed operation leaves knowledge paused for repair. Forgetting does not remove independent chats, pictures, exports or older backups; review the confirmation before applying it.

Notebook removal refuses linked, hard-linked, changed or excessively large trees. If cleanup fails after it starts, some notebook files may already have been removed; the knowledge state remains paused. See [Memory controls](MEMORY_CONTROLS.md) before retrying.

Backups include episodes, curation, pins, reflection settings and ledgers. Restore changes the dataset identifier, pauses reflection for review, cancels restored work and withholds stale derived text. Your retained story curation and pins remain stored. **Review restored moment** reveals them only after the current owned source text, hashes, ranges and original recall boundary match. Review the excerpts and explicitly choose **Restore saved curation**. This preserves the saved label, importance and pin; a generated candidate remains a candidate. Changed sources or a newer recall boundary cannot be overridden by this action. A conflicting current capture requires review rather than overwriting newer curation.

Companion/session removal clears affected episode/reflection content; minimal redacted reflection-job tombstones remain. Clearing all data removes these records too. Run one Altera process for an instance; restart recovery assumes the previous process has stopped. The queues and state gate do not support concurrent independent servers against one data folder.

[Memory controls](MEMORY_CONTROLS.md) · [Check-ins and rituals](PRESENCE.md) · [Backup and restore](BACKUP_AND_RESTORE.md) · [API](API_REFERENCE.md)
