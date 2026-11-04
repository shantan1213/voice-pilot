# 📧✅ Gmail + Tasks agent tools (Raala's second and third tools)

Gmail (send-only) and Google Tasks join Calendar behind the same registry and
executor workflow. There is still no Tools page: the user
speaks, the backend selects at most one registered action, validates it, and
speaks the real API result through the existing voice pipeline.

## Gmail: send-only

Scope: `https://www.googleapis.com/auth/gmail.send`. No inbox scope is
requested, so reading, searching, labeling, deleting, and bulk sending are
impossible by construction. A read request ("read my latest email") gets the
unsupported-action message.

| Action | Kind | Arguments |
|---|---|---|
| `send_email` | write (immediate) | `to` (1-10 valid emails), `subject`, `body` |

The MIME message is plain text, base64url-encoded, sent via
`POST /gmail/v1/users/me/messages/send`. Recipients are validated
(format, non-empty, max 10, no duplicates); subject and body must be
non-empty. Once all three fields are collected and validated, the message is
sent immediately without asking for a second confirmation. Spelled-out
addresses are error-prone over voice, so the UI offers one text box accepting
email details or a complete request, sent through the same chat pipeline.
Success is claimed only after Gmail's response (message ID). After Confirm, Raala first streams a progress line ("Sending your mail now...") while the send runs, then the success line. Both appear on screen and are spoken.

If you give the subject and message in one breath ("test mail saying hi how are you"), Raala splits them on cue words like "saying" or "message is" — the tail becomes the message — instead of stuffing everything into the subject. With no cue, the whole reply stays the subject, as before.

Whenever a mail body first appears — whether you answer "What should the message say?", give subject and message in one breath, or the selector extracts a complete request — the backend treats those words as the mail's purpose and drafts the actual wording with Muse Spark ([mail_drafter.py](../app/agent/mail_drafter.py)). Your subject is kept if you gave one, otherwise generated; the body is a few polite sentences keeping every fact you stated. The generated text is what gets confirmed and sent. Explicitly labelled dictation ("body: ...") stays verbatim, and any drafting failure falls back to your own words.

Partial turns accumulate in a per-session email draft (recipient, then
subject, then body). Each turn merges selector output with addresses and
subject:/message: labels found in the text; a short plain reply fills the
slot Raala just asked for, and anything unrecognized falls back to normal
chat with the draft kept. A different valid intent discards the draft,
"cancel" drops it explicitly, and drafts expire after 10 minutes.

## Tasks

Scope: `https://www.googleapis.com/auth/tasks`. Task-list deletion and bulk
operations are not supported.

| Action | Kind | Arguments |
|---|---|---|
| `list_task_lists` | read | none |
| `get_tasks` | read | `task_list_id` (default `@default`), `max_results` 1-100, `show_completed`, optional `due_min`/`due_max` |
| `get_task_details` | read | `task_list_id`, `task_id` |
| `create_task` | write (confirm) | `task_list_id`, `title`, optional `notes`, `due` |
| `update_task` | write (confirm) | `task_list_id`, `task_id`, plus at least one of `title`/`notes`/`due`/`status` |
| `complete_task` | write (confirm) | `task_list_id`, `task_id` (PATCH `status: completed`) |

Due dates are RFC 3339 with the local offset, never silently UTC. Reads are
verbalized by Muse Spark from the real response; writes speak deterministic
confirmation prompts and success/failure lines. 404 maps to `not_found`;
403 maps to a reconnect prompt (missing scope); 429 and 5xx map to structured
errors.

Known limitation: `update_task`, `complete_task`, and `get_task_details` need
a task ID (likewise `get_event_details` needs an event ID), which voice users
do not know. Today Raala lists tasks/events first and the user picks from the
spoken list; title-to-ID resolution in one turn is future work.

## Incremental authorization

`GET /auth/google/login` now requests all three scope sets at once with
`access_type=offline`, `prompt=consent`, and `include_granted_scopes=true`,
so re-running login preserves existing grants while adding the new ones.
If the stored refresh token predates Gmail/Tasks, the first Gmail/Tasks call
fails with a reconnect prompt instead of a raw 403 — the user re-opens the
login URL, Google shows only the missing consent, and the stored scope string
updates on exchange. Calendar keeps working throughout.

Each handler additionally checks the stored granted scopes before calling its
API, so a Calendar-only grant yields "Gmail/Tasks is not authorized yet"
guidance rather than an API error. Tokens still never reach the frontend or
Muse Spark.

Detection is automatic before selection time: on the first tool-related
turn, the chat gate checks the connection before the selection call and
slot-filling, so an unlinked account gets a tappable Connect Google link
(via an `action` stream event) instead of collecting details first and
hitting an API error at send time. Bare "send a mail" intents skip the
selector LLM call and go straight to slot-filling; intents carrying an
address, subject, or message still go through the selector for extraction.

## What did not change

The keyword prefilter now also listens for mail/task words
(`email`, `send`, `task`, `remind`, …); ordinary chat still costs one LLM call
and never touches Google. The confirmation store remains in use for Calendar
and mutating Tasks. Gmail sending bypasses confirmation by design; the
executor, SSE protocol, STT/TTS, history, and performance gates are otherwise
unchanged.

## Sheets (fourth, future)

Same recipe: add `app/integrations/google/sheets.py` with pure API functions
plus `register_sheets_tools(registry, deps)`, register it beside the other
three in `main.py`, request the Sheets scope in `oauth.py`, add Pydantic
models, mirror `tests/test_agent_gmail_tasks.py`, and document the actions
here. No dummy registrations.

Source: [`app/integrations/google/gmail.py`](../app/integrations/google/gmail.py),
[`app/integrations/google/tasks.py`](../app/integrations/google/tasks.py),
shared [`oauth.py`](../app/integrations/google/oauth.py) and
[`token_store.py`](../app/integrations/google/token_store.py),
gate in [`app/main.py`](../app/main.py),
tests in [`tests/test_agent_gmail_tasks.py`](../tests/test_agent_gmail_tasks.py).
