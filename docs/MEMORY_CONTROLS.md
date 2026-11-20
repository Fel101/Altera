# Memory sources and controls

Open **Memory** from chat and choose a companion. The page shows only that companion’s memories. **Default companion / legacy shared memories** is a separate scope. Switching this selector does not switch the companion you are chatting with.

- **Remember** on one of your chat messages opens an editable fact and shows the companion receiving it. It saves the message ID and a fingerprint of the source text. You can also add a fact directly on the Memory page.
- **Sources** shows the original message’s current location and whether the source is current, changed or unavailable. Message IDs survive session splits and merges. Repeated automatic confirmations retain their source links.
- Automatic extraction reads your own saved messages, in batches, after enough of them have gathered (four by default); it is not tied to a single turn. The companion's replies, imagined scenes and roleplay are never evidence. Each memory keeps links to the messages it came from, so Correct, Forget, Sources and inclusion counts work on it.
- When the chat model is unreachable or its answer cannot be read, the phrase matcher writes the memories instead. Those are marked **phrase fallback** on Memory and in the report. Rebuild automatic memories asks the model to read the eligible chat again; it falls back the same way if the model is still unavailable.
- **Correct** records your new assertion as a user correction. It does not pretend that the edited fact came from the old source message.
- **Forget** removes the chosen memory. **Forget all memories for this companion** affects only the selected companion.


Older memories with no tracked source remain visible and are marked for review. They are not supplied as recalled facts until you confirm them through Correct or rebuild automatic memories from the current eligible chat. The app does not invent source links for old entries.

## What she knows about me

On Memory, select a companion and choose **What she knows about me**. The report reads saved context directly, without asking a model to summarize it. Download the same report as JSON or Markdown. It includes current memories and their source roles/status/excerpts, notebook text with aggregate provenance, recall/repair state, bounded conversation-history candidates and applicable mood/legacy context.

Conversation candidates use the same recall boundary, compaction window and accepted-check-in validity filter as ordinary chat. An invalidated accepted check-in can remain visible in the conversation but is not listed as a prompt candidate. The report checks this without recovering interrupted presence jobs.

**Eligible** means the app permits an input; semantic search, compaction and token budgeting determine what a particular reply receives. This is not a transcript of the prompt sent to a model or a claim about a model's internal knowledge. A current source proves the message still matches its recorded fingerprint, not that extraction interpreted it correctly. A fact explicitly saved from an assistant message is labelled with that source role rather than presented as something you said.

Notebook entries can include manual edits and older derived material. Their source range describes the latest update, not citations for every statement. The report keeps this limitation visible. The default persona's older shared callbacks/moods are distinguished from source-linked memories; other companions' records and machine settings are excluded. Global profile/event/relationship stores are not presented as normal scoped chat facts. Unassigned legacy conversations remain under Default ownership in the report even if an older chat fallback can temporarily use an active persona; normal companion migration assigns those chats.

Reports briefly reserve a consistent read window. If replies, notes, pictures or another request are active, wait and retry after a 409 response. Downloads contain private plaintext and are separate from restore archives. The report's source-content reads do not change the recall boundary or repair knowledge.

Chat and notebook prompts now require evidence for personal facts and shared history, preserve uncertainty and prefer your explicit corrections. Imagined scenes, generated pictures and earlier assistant guesses are not treated as evidence of real events. These instructions reduce unsupported recall; they do not guarantee that a model will never make a mistake.

## What correction and forgetting change

Correction and forgetting clear the companion’s notebook, including manual note text and note history. They also set a fresh recall boundary across this companion’s conversations: earlier chat stays visible in the app but is no longer sent to the model. The confirmation explains this before you apply it. Other saved memories remain; still-valid memories retained before a boundary also survive later repairs.


Correction needs the configured local memory embedding model. Altera prepares the replacement embedding before changing the recall boundary or notebook; if local assets are unavailable, the saved memory and notebook remain intact. If a later vector write fails, the outcome may be uncertain and knowledge remains paused; the notebook may already have been cleared. Review the saved state before retrying. The local embedding adapter does not download a model or use a cloud fallback.

This prevents a forgotten fact from being immediately read back from the old conversation or an older notebook. The source conversation, pictures, other memories, downloaded exports and saved backups are not erased by forgetting one memory. Other copies of the same information can still exist in those places. Default-persona legacy callbacks are cleared or excluded after a recall reset because they have no source links.

New messages after the boundary can be remembered normally. The configured memory embedding function is used when saving or rebuilding vector memories; no cloud fallback is introduced by these controls.

## When conversation text changes

Editing, deleting or replacing a message pauses derived memories and notes for its companion. The chat links to **Review memories**. Choose **Rebuild automatic memories** to replace automatic entries from the current eligible text and clear stale notebook copies. Manually added/corrected memories remain. A manually saved memory whose source changed stays marked for review and excluded from recall until you correct or forget it.

Rebuilding asks the chat model to read the current eligible chat again, in batches, and write the automatic memories afresh. It runs in the background: chatting carries on, and derived memories and notes stay paused until it finishes. If the chat is edited while the model is reading, they stay paused and Memory asks for another rebuild. With the model unreachable it falls back to the local phrase matcher, and those memories are marked as such. Rebuilding never asks the model to rewrite your own added or corrected facts. The notebook is subsequently written afresh through the normal notes workflow. Notebook source details distinguish conversation ranges, user edits and earlier notes whose sources were not recorded.

Active replies and note writers must finish before a memory mutation can run; an active-work conflict returns 409 rather than queueing the operation. New requests are paused during the mutation. If a file or memory write fails, the operation is not reported successful; derived knowledge remains paused for repair.

The per-companion notebook cleanup used by correction and Forget checks the companion and notebook directories and plans a bounded tree before removing files. Links, reparse points, hard-linked files, unexpected file types, excessive nesting or changed entries stop the operation. A filesystem change or removal error after cleanup begins can leave a partially removed notebook; keep the affected files and review the paused knowledge state before retrying. These checks do not protect against every concurrent same-account filesystem change or securely erase old disk blocks and backups.

Conversations belonging to different companions cannot be merged. Splits retain ownership, message IDs and timestamps. Split/merge operations retire old request replay copies and pause derived knowledge for repair.

Proactive check-ins honor the same owner recall boundary. They skip generation when no eligible conversation remains and exclude stale shared mood/follow-up context from scoped companion prompts.

## Priority, episodes and reflection

**Retrieval priority** changes a memory's pin and importance (1–5) separately from its content, source eligibility and confidence. Age lowers ordering with a 30-day half-life and a 20% floor; pins suppress age decay, and no item is deleted by this score. Unknown legacy timestamps without an offset stay neutral. Context inclusion counts reflect completed saved reply inputs, not measured model attention or a guarantee the model mentioned a memory. Failed turns and report reads do not strengthen recency.

**Our story** keeps reviewed, source-tracked episodes with separate labels for textual exchanges, imagined scenes and derived summaries. Optional reflection has separate per-companion consent and adds source-tracked derived notebook entries without overwriting the manual notebooks. The knowledge report includes those current entries and exact original user-message excerpts. New ordinary notebook evidence is user-only; earlier derived or manually edited text retains its provenance limits.

Correction/Forget and source edits invalidate affected episodes, check-ins and reflection work. Explicit Forget clears current reflection notebook copies and reports too; pending job tombstones retain no copied conversation text. A restore retains your story curation and pins as stale material and requires explicit matching-source review before recall. See [Our story and reflection](OUR_STORY.md) for the complete behavior and limits.

## API

All mutation routes use the app’s origin and dataset-epoch checks. Scope IDs are companion IDs; `_default` is reserved for the default persona and cannot collide with a valid avatar ID.

| Method and path | Behavior |
| --- | --- |
| `GET /api/knowledge/scopes` | Companion choices and the active companion. |
| `GET /api/knowledge/messages/<id>` | Current source message and its companion scope. |
| `GET /api/knowledge/scopes/<scope>/memories` | Memories, source statuses, review flags, recall boundary and notebook provenance. |
| `POST …/memories` | Remember `content`, optional `type`, `confidence`, `source_message_id` and `session_id`. |
| `PUT …/memories/<id>` | Correct `content`, with optional `confidence`. |
| `PATCH …/memories/<id>/priority` | Change `pinned` and/or integer `salience` 1–5 with frozen `dataset_epoch`, priority `revision` and `content_sha256`; no recall reset or confidence change. |
| `DELETE …/memories/<id>` | Forget; JSON `confirm: "forget"`. |
| `DELETE …/memories` | Forget the scope; JSON `confirm: "forget-all"`. |
| `POST …/repair` | Rebuild; JSON `confirm: "rebuild"`. Returns `status: "started"` and runs in the background; the inventory's `rebuild` reports `running`, `done`, `changed` (edited meanwhile: rebuild again) or `error`. |
| `GET …/duplicates` | Review this scope’s current, usable duplicate candidates. |
| `POST …/duplicates` | Consolidate; JSON `confirm: "merge"`. User corrections and stale source entries are protected. |

The existing `/api/memory` CRUD routes use the same correction/forgetting behavior. GET defaults to the active companion; `avatar_id=` selects the default scope. Explicit owner reassignment through PUT clears foreign source metadata and resets recall for both affected companions. The older global clear endpoint still requires `confirm: "yes"`; the new page offers scoped clearing instead.
