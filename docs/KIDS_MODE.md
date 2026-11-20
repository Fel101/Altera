# Kids mode

**Kids mode is a guardrail, not a certification.** It is not a parental-control product and has not been tested or
approved as one. It makes a pal a better fit for a child to talk to, and it will still miss things. An adult needs to
keep an eye on things.

## Setting it up

Kids mode is locked until a grown-up sets it up, once, for this Altera:

1. On **Home**, open **Settings** under any pal and choose **Set up kids mode…**.
2. Read what it does and what it cannot do, tick **I understand that kids mode is a guardrail, not a guarantee**, choose
   a **parent PIN** (4 to 8 digits) and repeat it, and choose **Enable kids mode**.

The PIN is set with kids mode because the lock below exists for the same reason: a child with a tablet must not be able
to open an adult-capable person, change the settings or turn adult mode on. Turning adult mode on asks for the PIN even
in a browser that is already unlocked, and the kids view has no way to reach it at all.

After that, every pal has a **Kids mode for this pal** switch in its settings, and **Make a pal** offers it too. Pals in
kids mode carry a **Kids mode** badge on Home and in the chat. Switching a pal into or out of kids mode needs the PIN.

To lock it again, open **About kids mode** and choose **Lock kids mode**. That is refused while any pal is still in kids
mode, so switch those off first. Locking kids mode, changing the PIN and leaving the kids view also need the PIN.

## The app lock and the kids view

A pal in kids mode is one click from an adult-capable person unless something stops the click. Two things do, and both
are enforced on the server rather than only hidden in the page:

- **The lock screen.** When a parent PIN exists, a browser that has not entered it sees `/lock` with two ways in: the
  PIN (the whole app) or **Kids** (only kids pals). This is the optional app lock. It exists only because kids mode does,
  and a child's tablet can stay in the kids view without affecting a parent's phone that is already unlocked.
- **The kids view.** Only pals in kids mode are listed and reachable. Settings, Home tiles for anyone else, Memory,
  Search, adult mode, pictures, check-ins and workspace tools are not there, and the server refuses every other page and
  API call for that browser, so typing a URL does not get around it. Leaving the kids view, switching a pal's kids mode
  off, locking kids mode and changing the PIN all need the PIN.
- **Its own pal.** The kids view keeps the pal it has open in that browser's session. It never changes who the rest of
  the household opens with, so a parent's phone still opens on the person it was talking to.

Five wrong PIN tries bring a wait for every browser, not just the one that tried: a child cannot clear the wait by
opening another browser or clearing cookies. The longest wait is five minutes.

When something cannot be read (the database is busy or damaged), the lock and the kids view fail closed: a browser that
has not entered the PIN stays locked, the kids view reaches no one it cannot confirm is a kids pal, and a child's message
on a restricted topic stays out of mood analysis. Each case is written to the log.

The PIN is kept as a salted scrypt hash in the database, never in the clear and never in a log. Five wrong tries bring a
short wait, and repeated tries a longer one. Backups copy the database, so they carry the lock with them.

## What it does, for a pal in kids mode

- **The prompt.** The model is told it is talking with a child: child-friendly topics, simple words, no romance, no
  personal details, and a trusted grown-up for anything big or worrying (`core/kids.py`, `RULES`).
- **The child's messages.** A message on a restricted topic gets a fixed reply and never reaches the model. Restricted
  topics are self-harm, sexual content and romance aimed at the pal, drugs, alcohol and smoking, violence and weapons,
  gambling, and sharing or asking for personal details (an address, a phone number, a password, meeting in person).
  Self-harm gets its own reply, pointing to a trusted adult and the local emergency number. A message turned away like
  this is saved in the chat as usual, but it never becomes a memory and is not sent to the model for mood analysis
  either (the local keyword check is used instead).
- **The pal's replies.** Each reply is checked as a whole before it is shown or spoken, so it arrives in one piece
  rather than word by word. A reply on a restricted topic, or one asking the child for personal details or to keep a
  secret, is replaced with a friendly change of subject; the model's words are not saved.
- **Off:** web search (no tool is offered and none runs), check-ins and any other message the pal starts itself (the
  Check-ins switch is greyed out and the presence service refuses), new chat pictures, and adult mode, which counts as off in
  that chat whatever it is set to elsewhere.

Memory, notes and story stay ordinary switches, off by default as for every pal.

A pal in kids mode can have a [portrait from NPC Forge](PORTRAITS.md) and a living portrait. Altera hides the camera and
**Picture of us** request buttons for that pal and refuses new chat picture requests at the server. Existing chat pictures
stay in history until removed. The living portrait never shows the flirty face, whatever NPC Forge made.

The topic lists live in `core/safety.py` (`kids_topic`, `kids_reply_problem`). They are word lists: they catch the
obvious phrasings, not every phrasing, and they avoid everyday words a child uses innocently ("naughty", "comic strip",
"my sister has a boyfriend").

## What it does not do

- It does not stop someone who knows the PIN, or someone who can edit the files on this PC. They can unlock the app,
  change the settings or read every conversation. The PIN is a guardrail against a child clicking, not a security
  boundary against an adult with the machine.
- It does not make the device child-safe. Other apps, other devices and the browser's own history are outside it.
- It does not check what a child types against anything but the lists above, and it cannot catch every phrasing.
- The model can still get things wrong.

## A forgotten PIN

Anyone with access to the PC's files can remove it. With Altera stopped:

```
python scripts/reset_pin.py --home "C:\path\to\the\instance"
```

It deletes the PIN and its try counter and leaves kids mode, every pal's own switch and all conversations alone. Start
Altera and set a new PIN in **Home > Settings > About kids mode**. Someone who can run that script can already read
everything on this PC; the lock is not protecting against them.

## Where it is stored

The household switch is the `kids_mode` key in the database (`{enabled, enabled_at}`), so backups carry it. The parent
PIN is the `app_pin` key in the same table: a salted scrypt hash with its parameters, beside `app_pin_attempts` for the
try counter. Neither key holds a PIN in the clear, and `GET /api/lock` never returns them. Each pal's switch is
`kids: true` in its `data/avatars/<id>/pal.yaml` and in its `avatar.json`. Editing `pal.yaml` by hand can turn kids mode
on or off for that pal; check-ins stay off while it is on.
