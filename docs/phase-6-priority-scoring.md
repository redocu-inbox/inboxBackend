# Phase 6: priority scoring

This phase scores each synced message and shows that score on the message list and detail. The product is an inbox for construction companies. Outlook mail is synced with Microsoft Graph (GET only) into Postgres by the ARQ worker.

Live Outlook sync has not been run against a real mailbox yet (it is blocked on the frontend deploy). Stored fields may be null or messy. The scorer has to return a result anyway.

The Build Plan item is: "Backend phase 6: priority scoring. This phase scores incoming emails by priority."

This document is the contract. It does not add a migration, a worker change, or a scorer.

## Goal and scope

In scope:

- A `priority_score`, a `priority_band`, and `priority_reasons` for each message that the scorer has processed.
- Writing those values on the existing message row after a successful sync upsert.
- Scoring again when the same Graph message is upserted again, and a separate re-score job for rows already in Postgres.
- A deterministic rule scorer. Its inputs are stored message columns. It makes no network call and no LLM call.

Out of scope:

- Wiring any LLM, including Anthropic Haiku.
- Delta sync (`GraphClient.sync_inbox_delta` still raises `NotImplementedError`).
- Creating or renewing Graph subscriptions.
- Form matching, PDF fill, drafts, and sending mail.
- New routes, new query parameters, and new JSON keys on list or detail.
- Changing `$select` on the Graph message list, or storing To, CC, or importance.
- A VIP address book, a settings screen, or a new environment variable.

## Where the code is today

`GET /messages` and `GET /messages/{id}` already return `priority_score`, `priority_band`, and `priority_reasons`. The keys are frozen in `MessageResponse` (`app/api/messages.py`).

Only `priority_score` exists on `messages`. It is a nullable float with index `ix_messages_priority_score`. Sync never sets it, so it stays null. `_message_response` copies that column and leaves the other two fields on their model defaults: `priority_band` is `null`, and `priority_reasons` is `[]`. `conversation_id`, `suggested_form`, and `draft` stay null the same way.

`sort=priority` is already `priority_score DESC NULLS LAST`, then `received_at DESC`. Keep that order.

The worker job `sync_mailbox_delta` (`app/worker/tasks.py`) lists recent mail and upserts by `(connected_account_id, graph_id)`. The Graph call is:

```text
GET https://graph.microsoft.com/v1.0/me/messages
  ?$select=id,subject,bodyPreview,receivedDateTime,from,isRead,body
  &$top=25
  &$orderby=receivedDateTime desc
```

It follows `@odata.nextLink` for at most four pages. HTML is stored as plain text by `to_plain_text` (`app/services/graph/html_text.py`), capped at 100000 characters. Empty text is stored as null.

`LLM_PROVIDER` defaults to `stub`. `get_llm()` returns `StubLLM`, whose `complete` returns the fixed string `[stub llm]`. Any other provider raises `NotImplementedError`. Phase 6 does not call `get_llm()`.

## Frozen API contract

List and detail keep the same keys. A stored score of 50 is still the JSON number `50.0`.

| Field | JSON type | When the row is unscored | When the scorer has written it |
| ----- | --------- | ------------------------ | ------------------------------ |
| `priority_score` | number or `null` | `null` | number from `0.0` through `100.0` |
| `priority_band` | string or `null` | `null` | `low`, `medium`, or `high` |
| `priority_reasons` | array | `[]` | array of reason objects, possibly empty |

`priority_reasons` is never `null` in the response. An empty array covers two database states: the column is null (not scored), or the column is `[]` (scored, and no signal fired). Callers tell those apart with `priority_score` and `priority_band`. Both are null only when the row has not been scored.

The API returns the stored score unchanged. It does not clamp, and it does not invent a band from the score. A row that has `priority_score` 50 and a null band still returns `50.0`, `null`, and `[]`. That is the current list-contract test, and it has to keep passing. The scorer is what writes a band and a clamped score together.

Band values are exactly these lowercase strings:

| Band | Clamped score |
| ---- | ------------- |
| `high` | 70 or higher |
| `medium` | 40 through 69 |
| `low` | 39 or lower |

Thresholds are initial. Tune them after the live Outlook test. Do not add a fourth band in this phase.

Each reason object has exactly these keys:

| Key | Type | Meaning |
| --- | ---- | ------- |
| `code` | string | Stable snake_case id from the signal table |
| `label` | string | Fixed English label from that table |
| `weight` | integer | Points that signal added, before the 100 cap |

Order is weight descending, then `code` ascending. One signal fires at most once. Example:

```json
"priority_reasons": [
  {"code": "keyword_change_order", "label": "Change order", "weight": 22},
  {"code": "keyword_deadline", "label": "Deadline", "weight": 16},
  {"code": "keyword_urgent", "label": "Marked urgent", "weight": 16},
  {"code": "recent", "label": "Received in the last 2 days", "weight": 12},
  {"code": "unread", "label": "Unread", "weight": 8}
]
```

No new response fields. `priority_scored_at` stays off the API.

## Data model

James writes the migration. Chain it after the current head, `d8e2b4c71a05`. If `main` has moved, chain it after the new head. Do not edit older revisions.

Leave `priority_score` as it is: nullable float, index `ix_messages_priority_score`. Do not add a server default. Existing rows stay null.

Add three nullable columns on `messages`. Use no server default, so existing rows stay unscored.

| Column | Type | Null | Notes |
| ------ | ---- | ---- | ----- |
| `priority_band` | `VARCHAR(16)` | yes | Application string, same idea as `form_fill_jobs.status`. Not a Postgres enum. Check constraint: null or one of `low`, `medium`, `high` |
| `priority_reasons` | `JSONB` | yes | Null means not scored. `[]` means scored with no signals. Same JSONB type already used for `form_templates.tags` |
| `priority_scored_at` | `timestamptz` | yes | Clock used for that score. Not returned by the API |

No new index. List sorting already uses `ix_messages_priority_score`. Re-score loads by `connected_account_id`, which already has `ix_messages_connected_account_id`.

Downgrade drops the check constraint and these three columns. It does not change `priority_score`.

`Message` in `app/models/message.py` gains the three fields, all defaulting to null. `_message_response` reads them. A null `priority_reasons` column becomes `[]` in JSON. A stored list is returned as stored, in the stored order (the scorer writes the order above).

## When scoring runs

Scoring is a pure function of stored columns plus a clock. The worker calls it. The API does not score during `GET /messages`.

On the success path of `sync_mailbox_delta`, after `_upsert_message` has applied the stored columns and before the existing commit:

1. Score that same `Message` instance.
2. Compute the whole result first. Then assign `priority_score`, `priority_band`, `priority_reasons`, and `priority_scored_at` together.
3. Commit once, with the message text, the way the job already does.

`_upsert_message` should return the row it inserted or updated. A Graph item that `graph_message_fields` rejects is still skipped, and it is not scored.

The unique key stays `(connected_account_id, graph_id)`. Scoring replaces the four columns on that row. It does not insert a second message. Running it twice with the same stored columns and the same clock yields the same four values.

Every successful upsert scores again, including when only `is_read` or the body changed. Recency uses the clock, so a later upsert can move a message from `recent` to `this_week` or off both.

A scorer exception is a bug, not a Graph failure. Catch it, log the exception type and the message id, and leave the priority columns as they were before that attempt. A new row stays null. Do not mark `sync_status` failed and do not use `graph_unavailable`. The message text still commits, and the sync still goes idle when Graph succeeded. Do not log subject, body, or tokens.

### Re-score job

Add an ARQ job `rescore_mailbox_priority`. The argument is the connected-account id string, same style as the other jobs. Register it on `WorkerSettings.functions`. Do not add a cron entry, and do not enqueue it from `POST /messages/sync`, the OAuth callback, or the webhook.

The job loads that account. If the row is missing or `disconnected_at` is set, it returns without writing. Otherwise it scores every message with that `connected_account_id`, using the same function, and commits. It does not call Graph, refresh tokens, or change `sync_status`, `last_sync_at`, or `sync_generation`. On a per-message exception it leaves that row's priority columns unchanged and continues. When it writes a score it also sets `updated_at`.

Nothing in the API starts this job. It is how James backfills mail synced before the scorer existed, and how he applies a later weight change. The recent-message sync only revisits what Graph returns (at most four pages of 25). Older stored rows keep their last score until this job runs.

A disconnected mailbox is hidden by the API. This job does not score it and does not clear `disconnected_at`. The next successful sync after reconnect scores the messages that upsert touches.

## Rule scorer

Put the function in something like `app/services/priority/score.py`. It takes the stored fields and a `now` datetime, and returns the score, the band, the ordered reasons, and that `now`. Tests pass `now` in. Production passes `utcnow()`.

Inputs, and only these:

| Column | Use |
| ------ | --- |
| `subject` | Keyword text. Null is allowed |
| `body_preview` | Keyword text. Null is allowed |
| `body_text` | Keyword text. Already plain. Null is allowed. Only the first 8000 characters are scanned |
| `from_name` | Project-role signal. Null is allowed |
| `from_email` | Project-role signal and VIP domain. Null is allowed |
| `is_read` | Unread signal. Only an actual `false` counts. Null is not unread |
| `received_at` | Recency. The column is non-null |

`graph_id`, `user_id`, and the raw Graph payload are not inputs. The scorer does not read To, CC, or Graph `importance`, because this sync does not store them. A missing recipient list is neither "sent directly" nor "CC".

Direct vs CC is a useful construction signal, and it waits. Turning it on means storing `toRecipients` and `ccRecipients` and changing the Graph `$select`. That is a later change, after the live mailbox test. Phase 6 does not reserve a reason code for it, so the API does not grow a reason that never fires.

### How a score is built

Start at 0. Add the weight of each signal that fires. Clamp the sum to 0 through 100. That clamped number is `priority_score` (stored as a float, so 74 is JSON `74.0`).

Reasons keep the table weight even when the sum is over 100. The score can be lower than the sum of `weight`.

Then set the band from the clamped score, using the table in the API section. A scored message always has a band, including a score of `0.0` (`low`, reasons `[]`). `priority_scored_at` is `now`.

Normalize text before matching:

- Non-strings and whitespace-only strings are empty.
- Take the first 8000 characters of `body_text`, then normalize that slice. Subject and `body_preview` are normalized in full.
- Replace `<...>` with a space, then drop any remaining `<` or `>`.
- Lowercase. Treat hyphens as spaces. Delete `'` and `’`. Collapse whitespace.

A phrase matches with a non-letter boundary on each side. The last word may have one extra trailing `s`, so `change orders` matches `change order` and `RFIs` matches `rfi`. `architecture` does not match `architect`.

Recency, compared with `now`. If `received_at` is naive, treat it as UTC.

| Code | Fires when |
| ---- | ---------- |
| `recent` | `received_at` is from 48 hours before `now` through 1 hour after `now` |
| `this_week` | `recent` did not fire, and `received_at` is from 7 days before `now` through 1 hour after `now` |

A `received_at` more than an hour in the future matches neither. Boundaries are inclusive. `recent` and `this_week` do not both fire.

### Signals and weights

These weights are initial. Tune them after the live Outlook test. Keep them as named constants in the scorer module.

| Code | Label | Weight | Fires when |
| ---- | ----- | ------ | ---------- |
| `keyword_rfi` | RFI | 22 | `rfi` |
| `keyword_change_order` | Change order | 22 | `change order`, `potential change order`, or `pco` |
| `keyword_safety` | Safety | 20 | `stop work`, `osha`, or `near miss` |
| `keyword_deadline` | Deadline | 16 | `deadline`, `due by`, `due date`, `respond by`, or `response needed` |
| `keyword_urgent` | Marked urgent | 16 | `urgent`, `asap`, or `time sensitive` |
| `keyword_submittal` | Submittal | 14 | `submittal` |
| `keyword_payment` | Payment | 12 | `pay app`, `pay application`, `application for payment`, `lien`, `past due`, or `invoice` |
| `recent` | Received in the last 2 days | 12 | Recency rule above |
| `sender_vip` | VIP sender | 10 | Domain of `from_email` is in the VIP tuple |
| `unread` | Unread | 8 | `is_read` is `false` |
| `sender_project_role` | Project role | 8 | Role phrase on the From line |
| `this_week` | Received this week | 6 | Recency rule above |

Keyword signals search subject, body preview, and the body slice only.

`sender_project_role` searches `from_name` and the local part of `from_email` (the text before `@`), with dots and underscores treated as spaces. It does not search the domain or the body. Phrases: `project manager`, `superintendent`, `estimator`, `architect`, `owners rep`.

`sender_vip` compares the domain (text after the last `@`, lowercased) to a module-level tuple. Exact match only: `pm@vip.example` matches `vip.example`, and `pm@mail.vip.example` does not. The shipped tuple is empty, so the signal does not fire in production until domains are added after the live test. Tests pass a tuple in. This phase does not add `VIP` configuration to the environment or the database.

## Edge cases

Null or blank subject, preview, and body: keyword signals do not fire. Unread and recency still can. The function still returns a score and a band.

Null `from_name` and `from_email`: sender signals do not fire. Null is not a VIP and not a project role.

Null `is_read`: the unread signal does not fire. Sync already keeps a missing Graph `isRead` as null rather than false.

HTML leftovers: storage already runs `to_plain_text`, which drops tags and removes `<` and `>`. The scorer still strips leftover tags so a later writer cannot make `div` or `span` look like a keyword. `Please see the <b>RFI</b>` still counts as RFI.

Very long bodies: `body_text` is already capped at 100000 characters. The scorer reads 8000 of those. A keyword only past that cut does not fire. The same keyword in the subject still fires.

Non-English text: the keyword list is English. Other languages still get unread and recency when those inputs say so. This phase does not detect language and does not translate.

Duplicates: the same Graph id on one mailbox is one row. A second upsert replaces the priority columns. The same Graph id on two mailboxes is two rows, each scored from its own stored columns.

Messy Graph items that lack an id or a parseable `receivedDateTime` are dropped by `graph_message_fields` and never reach the scorer. An address with no `@`, or with whitespace, is stored as a null `from_email`.

## Test plan

Use fixture dicts and a fixed `now` of `2026-11-09T18:00:00Z`. Do not call Graph, `urlopen`, or `get_llm`. The pure function needs no database. Patch `app.services.llm.factory.get_llm` so a call fails the test. The re-score job and the pure function also fail the test if they call `app.worker.tasks._graph_get`.

`sync_mailbox_delta` may still call the existing `_graph_get` fake. It must not call `get_llm`.

Expected results at that `now`:

| Case | Inputs | Score | Band | Reasons, in order |
| ---- | ------ | ----- | ---- | ----------------- |
| Change order | Subject `URGENT: change order 18 due by Friday`. Body `Please approve this change order.` Unread. `received_at` `2026-11-09T16:00:00Z` | 74.0 | `high` | `keyword_change_order` 22, `keyword_deadline` 16, `keyword_urgent` 16, `recent` 12, `unread` 8 |
| RFI | Subject `RFI 12 — door hardware`. Body `See the attached sketch.` Unread. `received_at` `2026-11-09T16:00:00Z`. From `Pat Lee <pat@gc.example>` | 42.0 | `medium` | `keyword_rfi` 22, `recent` 12, `unread` 8 |
| Old invoice | Subject `Invoice 441`. Body null. Read. `received_at` `2026-10-01T12:00:00Z` | 12.0 | `low` | `keyword_payment` 12 |
| Submittal this week | Subject `Submittal for curtain wall`. Read. `received_at` `2026-11-04T18:00:00Z` | 20.0 | `low` | `keyword_submittal` 14, `this_week` 6. `recent` is absent |
| Null text, unread, old | All text null, from null, `is_read` false, `received_at` `2026-10-01T12:00:00Z` | 8.0 | `low` | `unread` 8 |
| Nothing fired | All text null, from null, `is_read` true, `received_at` `2026-10-01T12:00:00Z` | 0.0 | `low` | `[]` |
| Null `is_read` | Subject `Hello`, `is_read` null, old | 0.0 | `low` | `[]` |
| Spanish, recent, read | Subject `Solicitud de información`. `received_at` `2026-11-09T16:00:00Z` | 12.0 | `low` | `recent` 12 |
| Project role | `from_name` `Alex Rivera, Superintendent`. Subject `Site walk`. Read. `received_at` `2026-11-09T16:00:00Z` | 20.0 | `low` | `recent` 12, `sender_project_role` 8 |
| VIP override | `from_email` `pat@vip.example`, domains `("vip.example",)`, read, old, no keywords | 10.0 | `low` | `sender_vip` 10 |
| VIP default | Same message, domains `()` | 0.0 | `low` | `[]` |
| Cap | All ten signals in the note below | 100.0 | `high` | All ten reasons, weights uncapped, order in the note |

Cap note. Unread, `received_at` two hours before `now`, `from_name` `Project Manager`, subject `URGENT RFI and change order`, body `Submittal attached. Stop work until you respond by Friday. Lien and pay app are past due. There is a deadline.` Signals sum to 150. Stored score is 100.0. Reason order: `keyword_change_order` 22, `keyword_rfi` 22, `keyword_safety` 20, `keyword_deadline` 16, `keyword_urgent` 16, `keyword_submittal` 14, `keyword_payment` 12, `recent` 12, `sender_project_role` 8, `unread` 8.

Also cover:

- The same phrase in the subject and the body produces one reason.
- `architecture` does not match `architect`. `pm@mail.vip.example` does not match VIP domain `vip.example`.
- Body keyword only past character 8000 does not fire. The same keyword in the subject does.
- Leftover HTML `<b>RFI</b>` fires `keyword_rfi` and does not invent a keyword from the tag name.
- Upserting the same `(connected_account_id, graph_id)` twice leaves one row and replaces the priority columns.
- Existing fixture `GRAPH_ITEM` in `tests/test_mailbox_reliability.py`, after `graph_message_fields`, at this `now`: subject becomes `Invoice 12`, body `Hello`, unread, received `2024-06-01`. Score 20.0, band `low`, reasons `keyword_payment` 12 then `unread` 8. `test_sync_success_failure_and_superseded_generation` does not assert priority fields today and has to stay green. PR 3 adds the score assertion. It keeps using a fake `_graph_get`.

API tests for the mapping, with columns written directly:

- Null score, null band, null reasons column: `null`, `null`, `[]`.
- Score 50 and null band and null reasons: `50.0`, `null`, `[]`, and the existing priority sort order stays (higher score first, equal scores newer first, null scores last).
- Score 74.0, band `high`, and the change-order reason list: the JSON objects match the contract, and the key set of a list item is unchanged.

## Implementation split

James writes these two PRs. This spec is neither of them.

### PR 2: schema and API mapping

- Alembic revision as described under Data model, plus the `Message` fields.
- `_message_response` reads `priority_band` and `priority_reasons`.
- No scorer, no worker change, no call to `get_llm`, no environment or config edit.

Acceptance:

- Upgrade and downgrade succeed. Existing message rows keep a null score, a null band, and a null reasons column.
- List and detail match the three API rows in the test plan above.
- `sort=priority` is unchanged.
- Response keys are unchanged. `priority_reasons` is an array. `priority_scored_at` is absent.
- `test_message_list_contract_sort_and_total` passes without weakening its null-band assertion.

### PR 3: worker, rule scorer, and tests

- The pure scorer and the weights above.
- Call it from `sync_mailbox_delta` after each successful upsert, in that same transaction.
- `rescore_mailbox_priority` registered and not scheduled.
- Tests in the test plan. No new environment variable. `LLM_PROVIDER` stays `stub`.

Acceptance:

- The fixture table matches, including the cap at 100.0 and the empty VIP tuple.
- A second upsert does not create a duplicate message.
- A scorer exception leaves message text saved, leaves priority columns unchanged, and still lets a successful Graph sync finish idle.
- Re-score updates priority columns for one connected mailbox and does not call Graph or `get_llm`.
- A disconnected account is skipped.
- Sync tests still fake `_graph_get`. Neither the scorer nor the re-score job opens a socket.

## Deferred until the live Outlook test

The live sync has not been checked against a real mailbox. Until that passes, leave these as follow-ups:

- Changing weights, band cutoffs, or the keyword list to match real construction mail.
- Putting real company domains in the VIP tuple.
- Assuming sender, body, or `is_read` are usually present. Tests keep null fixtures.
- Storing recipients and adding a direct-vs-CC signal. Confirm from real payloads whether the current `$select` is enough.
- Keyword lists for languages other than English.
- Any product decision about backfill beyond the manual `rescore_mailbox_priority` job. The job is the only backfill this phase specifies.

## Deferred until the ZDR decision

[DEC-021](https://app.notion.com/p/40f6b731e7548182814de37379a770b2) proposes Anthropic with org zero-data-retention: Claude Haiku 5.5 (`claude-haiku-5-5`) for sorting, Sonnet 5.5 for form prefill. Status is **Proposed**. It is not a decision to send mail to a model.

Until that status is Decided, and until ZDR is confirmed for the org that would receive customer mail:

- The rule scorer is the only scorer.
- Do not call `get_llm()`, and do not branch on `LLM_PROVIDER`.
- Do not add an Anthropic client, a prompt, or a request that includes subject or body.
- Do not treat `StubLLM` as a scorer. Its return value is `[stub llm]`, not a score.

A later change, after ZDR is decided, can add a hosted scorer behind the existing `LLM_PROVIDER` setting. `stub` stays the default and stays local. That later change is not part of PR 2 or PR 3. Form prefill stays out of phase 6 even after ZDR is decided.
