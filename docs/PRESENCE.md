# Check-ins, rituals and expression

Open **Settings → Check-ins and rituals** for the current companion. Presence starts **off**, independently of the old proactive setting. Choose your timezone, review the connection, enable presence and save. Preferences bind one companion to its original conversation; opening another chat does not move or enable pending work.

Altera and the selected model service must remain running. Closing the browser is fine; closing Altera or sleeping the PC pauses generation. This feature is a saved inbox. A browser that has Altera open can also show a notification when a check-in arrives (see [Notifications](#notifications)); nothing reaches a closed browser.

## Schedule and limits

Defaults: UTC, quiet hours 22:00–08:00, six-hour check-in interval, six-hour cooldown between delivered items, two items per local day and two-hour expiry. Choose an IANA timezone such as `America/New_York`; Altera never guesses the server’s zone. Windows uses the declared `tzdata` dependency. Equal quiet endpoints mean quiet all day. A zero daily cap pauses delivery.

The UI exposes interval, quiet hours and daily cap; the API also exposes cooldown and expiry. Regular check-ins wait for the configured interval after conversation activity. Rituals avoid the first fifteen minutes after activity. Daily greetings, goodnights and weekly rituals share the same cap and cooldown. Quiet hours apply before and after generation. Missed schedules have bounded catch-up and expiry rather than a backlog.

A repeated daylight-saving wall time runs once, at its first occurrence. A missing wall time moves to the next valid minute. The scheduler checks every thirty seconds and holds no writer lease while idle. One job per companion can be pending/running, with one generation globally. Restart marks interrupted generation failed without automatic inference retry. **Check now** explicitly queues work with a stable retry ID and honors quiet hours/caps/cooldown. The daily cap and cooldown count delivered items, not failed generation attempts; a new explicit request after a failure can make another provider call.

Generation makes one provider request, with no SDK retries, a 256-token output budget, bounded response size and transport timeouts. A slow model can fail. There is no tool, photo, web-search or speech work and no provider fallback. Opening or refreshing the inbox never generates text.

## Read, dismiss or accept

A banner announces unread available messages. **Read** marks an item read without changing chat. **Dismiss** hides its text and prevents acceptance. **Add to conversation** saves the original text once in its original chat; retry returns the same saved message. It neither generates a replacement nor plays speech.

Expired, dismissed, failed and stale work stays labelled and cannot be accepted. Responses from a previous companion, hidden page or replaced dataset cannot update another chat. Read failures back off and pause after five failures; Refresh retries explicitly. There is no automatic offline sending.

## Notifications

**Settings → Notify me about check-ins** lets this browser show a notification when a check-in arrives for anyone, not
only the person whose chat is open. It is off until you switch it on, and the browser asks for permission then.

- **Only while Altera is open**, in a tab or as the installed app. Altera's page asks for new check-ins once a minute;
  a browser may ask less often for a tab in the background. Nothing reaches a closed browser or a sleeping phone:
  that would need Web Push, which goes through the browser maker's push service, and it is not built.
- **No message text by default.** A notification says only who has a check-in ("Mira has a check-in for you").
  **Show the message in the notification** adds the first 180 characters; anyone who can see the screen can then read
  it, on a phone's lock screen too.
- **Not while you are looking.** While Altera is in front, the banner already says it, so no notification is shown.
- **Nothing in discreet mode, and never for a kids pal.** A browser in the kids view or a locked browser gets no
  notifications, and switching them on there turns them off again.
- **Each check-in once.** A notification is shown when the check-in arrives; one that was read, dismissed, added to
  the chat, expired or made stale is not announced. Switching notifications on starts from that moment, so check-ins
  already waiting are not announced. Clicking a notification brings Altera to the front and opens that person (if Altera was closed since, it opens
  with whoever you talked to last).

The setting is kept in this browser (`altera_checkin_notices` in its storage), so each phone or computer chooses for
itself. Turning it off in Altera stops it; the browser's own site settings can also block notifications.

## Sources, privacy and recovery

Context contains the companion persona, bounded eligible memories and recent user words, and clean notebook summaries labelled as derived material. Instructions prohibit invented shared history, guilt and demands for attention; prompt rules cannot guarantee perfect model behavior.

Jobs capture owner/session/dataset identity, preference/knowledge revisions, recall boundary, source message IDs/hashes and context/connection fingerprints. Publication, display and acceptance recheck them. Edited/forgotten/deleted sources or changed connection settings make the item stale. Stale accepted text remains visible in chat but is excluded from future prompts, including after split/merge. Generated inbox text is always excluded from notebook consolidation, even while current, so a generated summary cannot promote it into new evidence.

Loopback providers are eligible after the main opt-in. External providers need separate approval for the displayed provider/model/connection. Changing endpoint, model or credentials requires renewed approval. Redirects and inherited environment proxies are disabled. Presence can be disabled with a broken connection; clear network approval if necessary.

Restore pauses presence, invalidates restored jobs/inbox and clears expression. Review settings before enabling it again. Conversation deletion scrubs its inbox text/snapshots; minimal acceptance tombstones prevent stale prompt reuse. Companion removal clears its preferences/rituals. Global clear removes all presence/expression rows. Existing archives/logs have separate retention. [Encrypted backups](BACKUP_AND_RESTORE.md) are available explicitly; ordinary storage and plaintext ZIP exports remain unencrypted unless covered by an accepted storage-protection deployment.

## Simulated expression

A conservative heuristic recognizes wording in a completed saved assistant reply, such as “I’m glad” or “I’m curious.” The companion-specific cue stores source IDs/hashes without another copy of the exchange. It is a simulated presentation state, separate from user emotion and factual memory.

Intensity halves every six hours and returns toward calm. Time away never creates resentment, hurt or demands. Changed sources, dirty knowledge, Forget and restore return it to calm. It can lightly color tone and select an available living-portrait frame. Missing frames use rest; brief reply expressions and thinking/speech animation take priority over the persistent cue.

Fake-clock/provider tests establish storage, scheduling and cancellation boundaries. Real model quality, microphone/echo acceptance and live activation remain separate checks.

[User guide](README.md) · [Voice](VOICE.md) · [Memory controls](MEMORY_CONTROLS.md) · [Backup and restore](BACKUP_AND_RESTORE.md)
