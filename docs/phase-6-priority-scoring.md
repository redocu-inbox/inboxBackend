# Phase 6: priority scoring and classification

This phase classifies each synced message, extracts a few key fields, and scores the message. The category and the due date feed the score. The product is an inbox for construction companies. Classifying construction mail is the core of the product, in the same phase as priority, not a later add-on.

Outlook mail is synced with Microsoft Graph (GET only) and Gmail mail is synced read-only. Both land in Postgres through the ARQ worker, on the same `messages` row. Tags live only in this database. This phase does not move, label, or otherwise write to the user's mailbox.

Live sync has not been run against a real mailbox yet (it is blocked on the frontend deploy). Stored fields may be null or messy. The classifier and the scorer have to return a result anyway.

The Build Plan item was: "Backend phase 6: priority scoring. This phase scores incoming emails by priority." Classification and key-field extraction are added to that item here. The filename stays `docs/phase-6-priority-scoring.md` so the existing link keeps working.

This document is the contract. It does not add a migration, a worker change, a scorer, a classifier, or a route.

The category list below is an initial proposal. **Pending founder confirmation.**

## Goal and scope

In scope:

- A `priority_score`, a `priority_band`, and `priority_reasons` for each message that the scorer has processed.
- A category, a category source, a category confidence, and extracted key fields for each message that the classifier has processed.
- A flag on the message for whether a user has corrected the classification.
- Writing those values on the existing message row after a successful sync upsert, for Outlook and for Gmail.
- Classifying and scoring again when the same provider message is upserted again, and a separate re-score job for rows already in Postgres.
- A deterministic rule classifier and a deterministic rule scorer. Their inputs are stored message columns. They make no network call and no LLM call.
- One correction route, in PR 4, that stores the original value, the corrected value, and a timestamp. Those rows are the labeled training set. Nothing in this phase trains a model.

Out of scope:

- Wiring any LLM, including Anthropic Haiku. An LLM classifier is a later phase, behind a feature flag, and only after DEC-021 is Decided.
- Reading or parsing attachments. That is a later phase.
- Delta sync (`GraphClient.sync_inbox_delta` still raises `NotImplementedError`).
- Creating or renewing Graph subscriptions.
- Form matching, PDF fill, drafts, and sending mail.
- A new list query parameter, including a category filter.
- Changing `$select` on the Graph message list, changing the Gmail `format=full` read, or storing To, CC, or importance.
- A write to Graph or Gmail: no folder move, no label, no `messages.modify`, no category written back to the mailbox.
- A VIP address book, a settings screen, or a new environment variable.
- A paid API, a hosted model, or any new service. The rule path runs in the existing API, worker, and Postgres.

`priority_score`, `priority_band`, and `priority_reasons` keep the contract in the next section. Classification adds keys beside them. It does not change the type, the null behavior, or the reason-object shape of those three fields.

## Where the code is today

`GET /messages` and `GET /messages/{id}` already return `priority_score`, `priority_band`, and `priority_reasons`. The keys are frozen in `MessageResponse` (`app/api/messages.py`). They do not yet return a category or any extracted field.

`tests/test_frontend_connect.py` locks the list key set in `LIST_KEYS` and the detail key set as `LIST_KEYS` plus `body_text` and `draft`. `assert set(item) == LIST_KEYS` fails if a key appears or disappears. PR 2 extends that set with the classification keys below. The priority assertions in `test_message_list_contract_sort_and_total` stay: a stored score of 50 with a null band still returns `50.0`, `null`, and `[]`.

Only `priority_score` exists on `messages`. It is a nullable float with index `ix_messages_priority_score`. Sync never sets it, so it stays null. `_message_response` copies that column and leaves the other two fields on their model defaults: `priority_band` is `null`, and `priority_reasons` is `[]`. `conversation_id`, `suggested_form`, and `draft` stay null the same way.

`sort=priority` is already `priority_score DESC NULLS LAST`, then `received_at DESC`. Keep that order.

The worker job `sync_mailbox_delta` (`app/worker/tasks.py`) upserts by `(connected_account_id, graph_id)` through `_upsert_message`. Microsoft and Google each have their own loop and their own commit. Both loops call `_upsert_message`. `_upsert_message` returns `None` today. A mapped item that the provider mapper rejects is skipped and is not classified.

The Graph call is:

```text
GET https://graph.microsoft.com/v1.0/me/messages
  ?$select=id,subject,bodyPreview,receivedDateTime,from,isRead,body
  &$top=25
  &$orderby=receivedDateTime desc
```

It follows `@odata.nextLink` for at most four pages. HTML is stored as plain text by `to_plain_text` (`app/services/graph/html_text.py`), capped at 100000 characters. Empty text is stored as null. Attachments are not in `$select`.

A `provider` of `google` never calls Graph. The job lists `users.messages` with `labelIds=INBOX` and `maxResults=25`, follows `nextPageToken` for at most four pages, then GETs each id with `format=full`. The Gmail id is stored in `graph_id`. `gmail_message_fields` (`app/services/google/messages.py`) writes the same stored columns as Graph. Parts that have a filename are skipped, so attachment bodies are not stored. The Gmail scope in use is `gmail.readonly`.

`GRAPH_SCOPES` still lists `Mail.ReadWrite` next to `Mail.Read`. This phase does not change that setting, and it does not call a Graph write. Categories are columns on `messages` plus rows in the correction log.

The Alembic head on `main` is `f5a2c8d14e60`. The priority-only draft of this spec named `d8e2b4c71a05`. That revision is no longer the head.

`LLM_PROVIDER` defaults to `stub`. `get_llm()` returns `StubLLM`, whose `complete` returns the fixed string `[stub llm]`. Any other provider raises `NotImplementedError`. Phase 6 does not call `get_llm()`.

## Frozen priority contract

List and detail keep these three keys, with these types. A stored score of 50 is still the JSON number `50.0`.

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

Thresholds are initial. Tune them after the live mailbox test. Do not add a fourth band in this phase.

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

`priority_scored_at` stays off the API. Category and due-date signals are more objects in this same array. They use new `code` values. They do not change this shape.

## Classification contract

List and detail gain the keys in this table. PR 2 adds them. Until a classifier or a user has written the row, the JSON is nulls and `user_corrected: false`.

| Field | JSON type | When the row is unclassified | When classification has been written |
| ----- | --------- | ---------------------------- | ------------------------------------ |
| `category` | string or `null` | `null` | one of the category strings below, or `null` when the rules ran and no rule matched |
| `category_source` | string or `null` | `null` | `rule` or `user` |
| `category_confidence` | number or `null` | `null` | `0.0`, `0.5`, or `1.0` |
| `project_name` | string or `null` | `null` | string or `null` |
| `project_number` | string or `null` | `null` | string or `null` |
| `due_date` | string or `null` | `null` | `YYYY-MM-DD`, or `null` |
| `responsible_party` | string or `null` | `null` | string or `null` |
| `reference_number` | string or `null` | `null` | string or `null`, for example `RFI 042` |
| `user_corrected` | boolean | `false` | `true` after any saved correction, otherwise `false` |

`user_corrected` is never `null` in the response. A null column becomes `false`. Callers tell "not classified" from "classified, no rule matched" with `category_source`: `null` means the classifier has not run, and `rule` with a null `category` means it ran and no rule matched. `category_confidence` is `0.0` in that second case.

`due_date` is a calendar date with no time and no offset. The API does not invent one from a weekday.

Category strings are exactly these lowercase values. **Pending founder confirmation.**

| `category` | Covers |
| ---------- | ------ |
| `submittal` | Submittals |
| `rfi` | Requests for information |
| `change_order` | Change orders, including a potential change order |
| `invoice` | Invoices and pay applications. One category, not two |
| `meeting_notes` | Meeting minutes and meeting notes |
| `safety` | Safety notices |
| `schedule` | Project schedules and look-aheads |
| `other` | None of the above, when a user says so |

The rule classifier does not write `other`. No match leaves `category` null. A user may correct a message to `other`.

`category_source` is `rule` when the current category was written by the rule classifier, and `user` when a correction set the category. `category_confidence` is `1.0` for a single matching category, `0.5` when two or more category rule groups match (the winner is still stored), and `0.0` when the rules ran and no group matched. A user-set category is `1.0`. The API returns the stored number. It does not recompute it.

`user_corrected` is `true` when the correction log has at least one row for that message. It covers a correction to the category or to any extracted field.

Example, after the rule classifier, for a message whose subject is `RFI 042 door hardware` and whose body contains a project and a due date:

```json
{
  "category": "rfi",
  "category_source": "rule",
  "category_confidence": 1.0,
  "project_name": "Harbor School",
  "project_number": "24-118",
  "due_date": "2026-11-11",
  "responsible_party": "Superintendent",
  "reference_number": "RFI 042",
  "user_corrected": false
}
```

No new list filter. `sort` stays `received_at` or `priority`.

## How category and due date join the score

The scorer reads the stored `category` and the stored `due_date` after classification. It does not parse them again from the text. Keyword signals still scan the text, with the same phrases and the same weights as before. A category signal and a keyword signal can both fire. An RFI whose due date is near adds the RFI category weight and a due-date weight on top of whatever keyword and recency signals already fired, so that message scores higher than the same RFI with no due date.

`meeting_notes`, `other`, and a null category add no category reason. A null due date adds no due-date reason. A due date more than seven days after the clock's UTC date adds no due-date reason. At most one due-date reason fires.

The band cuts stay 70 / 40. The clamp stays 0 through 100. Reason order stays weight descending, then `code` ascending.

The original fixture table in the test plan is the scorer result when `category` is null and `due_date` is null. Those inputs are how PR 3 keeps that table green. The worker writes a category before it scores, so the worker-level numbers are the combined table further down.

## Data model

James writes the migrations. Chain the first one after the current head, `f5a2c8d14e60`. If `main` has moved, chain it after the new head. Do not edit older revisions.

Leave `priority_score` as it is: nullable float, index `ix_messages_priority_score`. Do not add a server default. Existing rows stay null.

PR 2 adds these nullable columns on `messages`. Use no server default, so existing rows stay unscored and unclassified.

| Column | Type | Null | Notes |
| ------ | ---- | ---- | ----- |
| `priority_band` | `VARCHAR(16)` | yes | Application string, same idea as `form_fill_jobs.status`. Not a Postgres enum. Check constraint: null or one of `low`, `medium`, `high` |
| `priority_reasons` | `JSONB` | yes | Null means not scored. `[]` means scored with no signals. Same JSONB type already used for `form_templates.tags` |
| `priority_scored_at` | `timestamptz` | yes | Clock used for that score. Not returned by the API |
| `category` | `VARCHAR(32)` | yes | Check constraint: null or one of the eight category strings |
| `category_source` | `VARCHAR(16)` | yes | Check constraint: null, `rule`, or `user` |
| `category_confidence` | double precision | yes | Check constraint: null or a number from 0 through 1 |
| `project_name` | `TEXT` | yes | |
| `project_number` | `VARCHAR(64)` | yes | |
| `due_date` | `date` | yes | Calendar date. No time zone |
| `responsible_party` | `TEXT` | yes | |
| `reference_number` | `VARCHAR(64)` | yes | |
| `user_corrected` | `BOOLEAN` | yes | Null until a correction. The API returns `false` for null |

No new index. List sorting already uses `ix_messages_priority_score`. Re-score loads by `connected_account_id`, which already has `ix_messages_connected_account_id`. This phase does not filter by category, so it does not index `category`.

PR 2 downgrade drops the check constraints and these columns. It does not change `priority_score`.

`Message` in `app/models/message.py` gains the fields, all defaulting to null. `_message_response` reads them. A null `priority_reasons` column becomes `[]` in JSON. A stored list is returned as stored, in the stored order (the scorer writes the order above). A null `user_corrected` becomes `false`. A stored `due_date` becomes `YYYY-MM-DD`.

PR 4 adds `message_classification_corrections`. Chain that revision after the PR 2 revision.

| Column | Type | Null | Notes |
| ------ | ---- | ---- | ----- |
| `id` | UUID | no | Primary key |
| `message_id` | UUID | no | Foreign key to `messages.id`, `ON DELETE CASCADE`. Index this column |
| `user_id` | UUID | no | Foreign key to `users.id`, `ON DELETE CASCADE` |
| `field` | `VARCHAR(32)` | no | One of `category`, `project_name`, `project_number`, `due_date`, `responsible_party`, `reference_number` |
| `original_value` | `TEXT` | yes | Stored text at correction time. Null stays null, not the string `null` |
| `corrected_value` | `TEXT` | yes | What the user saved. A date is `YYYY-MM-DD` |
| `created_at` | `timestamptz` | no | The correction timestamp |

Rows are inserts. This phase has no update and no delete for the log. One changed field is one row. A request that changes three fields writes three rows with the same `created_at`. Downgrade drops the table.

Deleting a user already cascades to messages. Corrections cascade with the message, and they also reference the user. The correction route is the only writer.

## When classification and scoring run

Classification and scoring are pure functions of stored columns. Scoring also takes the stored category, the stored due date, and a clock. The worker calls them. `GET /messages` does not classify or score.

On the success path of both provider loops inside `sync_mailbox_delta`, after `_upsert_message` has applied the stored columns and before that loop's commit:

1. `_upsert_message` returns the row it inserted or updated.
2. Run the rule classifier on that row.
3. Apply the classifier result onto the row, skipping any field that already has a correction-log row for this message. PR 3 has no log, so it applies every field. PR 4 adds the skip.
4. When `category` is not skipped, also write `category_source` `rule` and the returned confidence. When `category` is skipped, leave `category_source` and `category_confidence` as stored.
5. Score that same `Message` instance from the columns it has after that apply.
6. Compute the whole priority result first. Then assign `priority_score`, `priority_band`, `priority_reasons`, and `priority_scored_at` together.
7. Commit once, with the message text, the way the job already does.

A provider item that `graph_message_fields` or `gmail_message_fields` rejects is still skipped, and it is not classified.

The unique key stays `(connected_account_id, graph_id)`. Classification and scoring replace columns on that row. They do not insert a second message. Running them twice with the same stored text, the same unlocked fields, and the same clock yields the same values.

Every successful upsert classifies and scores again, including when only `is_read` or the body changed. Recency and the due-date window use the clock, so a later upsert can move a message between due-date reasons, or from `recent` to `this_week`.

A corrected field survives that upsert. The log is the lock. `user_corrected` stays true. The scorer still runs and uses the locked category and the locked due date.

A classifier exception is a bug, not a provider failure. Catch it, log the exception type and the message id, leave the classification columns as they were before that attempt, and still score from those stored values. A scorer exception is also a bug. Catch it, log the exception type and the message id, and leave the priority columns as they were before that attempt. A new row stays null. Do not mark `sync_status` failed and do not use `graph_unavailable` or `gmail_unavailable`. The message text still commits, and the sync still goes idle when the provider call succeeded. Do not log subject, body, extracted values, or tokens.

### Re-score job

Add an ARQ job `rescore_mailbox_priority`. The argument is the connected-account id string, same style as the other jobs. Register it on `WorkerSettings.functions`. Do not add a cron entry, and do not enqueue it from `POST /messages/sync`, the OAuth callback, or the webhook.

The job loads that account. If the row is missing or `disconnected_at` is set, it returns without writing. Otherwise it classifies and scores every message with that `connected_account_id`, using the same functions and the same correction locks, and commits. It does not call Graph, Gmail, refresh tokens, or change `sync_status`, `last_sync_at`, or `sync_generation`. On a per-message exception it leaves that row's priority and classification columns unchanged and continues. When it writes a score it also sets `updated_at`.

Nothing in the API starts this job. It is how James backfills mail synced before the classifier existed, and how he applies a later weight or rule change. The recent-message sync only revisits what the provider returns (at most four pages of 25). Older stored rows keep their last result until this job runs.

A disconnected mailbox is hidden by the API. This job does not classify it and does not clear `disconnected_at`. The next successful sync after reconnect classifies the messages that upsert touches.

## Rule classifier

Put the function in something like `app/services/priority/classify.py`. It takes the stored fields and returns the category, the confidence, and the extracted fields. It does not take a clock. It does not write the database. Tests call it with plain dicts. The worker is what writes `category_source`.

Inputs, and only these:

| Column | Use |
| ------ | --- |
| `subject` | Rules and extraction. Null is allowed |
| `body_preview` | Rules, and extraction after subject and `body_text`. Null is allowed |
| `body_text` | Rules and extraction. Already plain. Null is allowed. Only the first 8000 characters are scanned |
| `from_name` | Responsible party. Null is allowed |
| `from_email` | Responsible party. Null is allowed |

Category rules use the same text normalization as the scorer, on subject, body preview, and the body slice. Extraction of the project name, the project number, the due date, and the reference number uses the plain subject, then the plain body slice, then the plain body preview, before hyphen-to-space normalization, so `24-118` and `2026-11-11` keep their hyphens. Compare those patterns without case sensitivity. The body wins over the preview when both contain a value, because the body is searched first.

### Which category wins

A rule group matches if any of its phrases matches. Phrases use the scorer's boundary rule, including the optional trailing `s`, except `co #`, which is specified below. Groups:

| Category | Phrases on the normalized text |
| -------- | ------------------------------ |
| `safety` | `stop work`, `osha`, `near miss` |
| `change_order` | `change order`, `potential change order`, `pco`, and the `co #` pattern |
| `rfi` | `rfi` |
| `submittal` | `submittal` |
| `invoice` | `invoice`, `pay app`, `pay application`, `application for payment` |
| `schedule` | `look ahead`, `schedule update`, `updated schedule`, `project schedule`, `construction schedule`, `baseline schedule` |
| `meeting_notes` | `meeting minutes`, `meeting notes`, `minutes of the meeting`, `minutes of meeting` |

`co #` is the token `co` with a non-letter boundary, then optional space, then `#`, then optional space, then a number. Bare `co` does not match. `company` does not match. `lien` and `past due` stay payment keywords for the scorer and do not select `invoice`.

If one group matches, the category is that group and confidence is `1.0`. If several match, walk this order and keep the first: `safety`, `change_order`, `rfi`, `submittal`, `invoice`, `schedule`, `meeting_notes`. Confidence is `0.5`. If none match, category is null and confidence is `0.0`. The classifier still returns that result.

### Reference number

Look only at the winning category. Search the plain subject, then the plain body slice, then the plain body preview. Optional `#` and spaces may sit between the cue and the number. A number is 1 to 12 digits with an optional single trailing letter. Keep leading zeros. If the cue matches and no number is present, the reference is null.

| Winning category | Cue | Stored value |
| ---------------- | --- | ------------ |
| `change_order` | `co #` and a number | `CO <number>` |
| `change_order` | `pco` and a number | `PCO <number>` |
| `change_order` | `change order` and a number | `CO <number>` |
| `rfi` | `rfi` and a number | `RFI <number>` |
| `submittal` | `submittal` and a number | `Submittal <number>` |
| `invoice` | `invoice` and a number | `Invoice <number>` |
| `invoice` | `pay app`, `pay application`, or `application for payment`, and a number | `Pay App <number>` |

The first cue that has a number wins. `schedule`, `meeting_notes`, `safety`, and a null category store a null reference. `safety` can still be the winning category when the text also contains `RFI 042`; the reference stays null because it is not taken from a losing group.

### Project, due date, and responsible party

Project number. On the plain subject, then the plain body slice, then the plain body preview, a cue is `project number`, `project no.`, `project no`, `project #`, `job number`, `job no.`, `job no`, or `job #`. Then optional `:` or `-` and whitespace. Then a token of length 2 through 32: a letter or digit, then letters, digits, `.`, `/`, `_`, or `-`. Store that token trimmed, with its original case and hyphens. The first cue wins.

Project name. Cue `project name`, then `:` or `-`, then the rest of that line. Trim, collapse internal whitespace, and cap at 120 characters. If the remainder is empty, `project_name` is null. Do not treat a bare `project:` as a name. Subject first, then body, then preview.

Due date. On the plain subject, then the plain body slice, then the plain body preview, a cue is `due by`, `due on`, `due date`, `respond by`, `response needed by`, `needed by`, or `deadline`. After the cue, skip up to 20 characters of space, `:`, or `-`, and read one date. The first valid date wins. Accepted forms:

- `YYYY-MM-DD`
- `M/D/YYYY` or `MM/DD/YYYY`
- `M/D/YY` or `MM/DD/YY`. Years 00 through 69 are 2000 through 2069. Years 70 through 99 are 1970 through 1999.
- `Month D, YYYY` and `Month D YYYY`. Month is the English name or its three-letter abbreviation.

An impossible date such as February 31 is skipped, and the search continues. A weekday, `today`, or `tomorrow` is a date only when `received_at` is present. It is resolved from that calendar day: `today` is the received day, `tomorrow` is the next day, and `Friday` is the same day when the message arrived on Friday, otherwise the next Friday. `by 5pm today` uses the same rule. Without `received_at`, those words are not a date. No cue, or no valid date, leaves `due_date` null.

Responsible party. Search `from_name` and the local part of `from_email` (the text before `@`), with dots and underscores treated as spaces. Use the scorer's normalization. Do not search the domain or the body. The first phrase in this order wins, and the stored value is the canonical string:

| Phrase | Stored |
| ------ | ------ |
| `project manager` | `Project manager` |
| `superintendent` | `Superintendent` |
| `estimator` | `Estimator` |
| `architect` | `Architect` |
| `owners rep` | `Owner's rep` |

No phrase leaves `responsible_party` null. A user correction may store a person's name instead. The rule does not.

## Rule scorer

Put the function in something like `app/services/priority/score.py`. It takes the stored fields and a `now` datetime, and returns the score, the band, the ordered reasons, and that `now`. Tests pass `now` in. Production passes `utcnow()`.

Inputs:

| Column | Use |
| ------ | --- |
| `subject` | Keyword text. Null is allowed |
| `body_preview` | Keyword text. Null is allowed |
| `body_text` | Keyword text. Already plain. Null is allowed. Only the first 8000 characters are scanned |
| `from_name` | Project-role signal. Null is allowed |
| `from_email` | Project-role signal and VIP domain. Null is allowed |
| `is_read` | Unread signal. Only an actual `false` counts. Null is not unread |
| `received_at` | Recency. The column is non-null |
| `category` | Category signal. Null, `meeting_notes`, and `other` add no points |
| `due_date` | Due-date signal. Null adds no points |

`graph_id`, `user_id`, and the raw provider payload are not inputs. The scorer does not read To, CC, or Graph `importance`, because this sync does not store them. A missing recipient list is neither "sent directly" nor "CC".

Direct vs CC is a useful construction signal, and it waits. Turning it on means storing recipients and changing the Graph `$select`. That is a later change, after the live mailbox test. Phase 6 does not reserve a reason code for it, so the API does not grow a reason that never fires.

### How a score is built

Start at 0. Add the weight of each signal that fires. Clamp the sum to 0 through 100. That clamped number is `priority_score` (stored as a float, so 74 is JSON `74.0`).

Reasons keep the table weight even when the sum is over 100. The score can be lower than the sum of `weight`.

Then set the band from the clamped score, using the table in the priority section. A scored message always has a band, including a score of `0.0` (`low`, reasons `[]`). `priority_scored_at` is `now`.

Normalize text before keyword matching:

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

Due date, compared with the UTC calendar date of `now`. Boundaries are inclusive. Only one of these fires.

| Code | Fires when |
| ---- | ---------- |
| `due_overdue` | `due_date` is before that date |
| `due_soon` | `due_date` is that date through 3 days after it |
| `due_within_7_days` | `due_soon` did not fire, and `due_date` is 4 through 7 days after that date |

### Signals and weights

These weights are initial. Tune them after the live mailbox test. Keep them as named constants in the scorer module.

Keyword, recency, and sender weights are unchanged:

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

Category and due-date weights are new. They fire from the stored columns, not from a second phrase scan:

| Code | Label | Weight | Fires when |
| ---- | ----- | ------ | ---------- |
| `due_overdue` | Overdue | 25 | Due-date rule above |
| `due_soon` | Due within 3 days | 14 | Due-date rule above |
| `category_safety` | Safety notice | 12 | `category` is `safety` |
| `category_rfi` | RFI | 10 | `category` is `rfi` |
| `category_change_order` | Change order | 10 | `category` is `change_order` |
| `category_submittal` | Submittal | 8 | `category` is `submittal` |
| `category_schedule` | Schedule | 8 | `category` is `schedule` |
| `due_within_7_days` | Due within 7 days | 8 | Due-date rule above |
| `category_invoice` | Invoice | 6 | `category` is `invoice` |

`meeting_notes` and `other` have no row. They add no reason.

Keyword signals search subject, body preview, and the body slice only.

`sender_project_role` searches `from_name` and the local part of `from_email` (the text before `@`), with dots and underscores treated as spaces. It does not search the domain or the body. Phrases: `project manager`, `superintendent`, `estimator`, `architect`, `owners rep`.

`sender_vip` compares the domain (text after the last `@`, lowercased) to a module-level tuple. Exact match only: `pm@vip.example` matches `vip.example`, and `pm@mail.vip.example` does not. The shipped tuple is empty, so the signal does not fire in production until domains are added after the live test. Tests pass a tuple in. This phase does not add `VIP` configuration to the environment or the database.

## User correction

PR 4 adds this route. PR 2 and PR 3 do not register it.

`PATCH /messages/{message_id}/classification`

Auth is the current user, same as the other message routes. Another user's message, a disconnected mailbox's message, and an unknown id are `404` with detail `Message not found.` A non-UUID is `422`. CORS already allows `PATCH`. This route does not change `allow_methods`.

The body is a JSON object. At least one of these keys is required. Unknown keys are `422`. A missing key leaves that field unchanged. A present key replaces it. A string is trimmed. An empty string is null. Over-long values are `422`, not truncated: `project_number` and `reference_number` at 64, `project_name` at 120, `responsible_party` at 200.

| Key | Accepted value |
| --- | -------------- |
| `category` | `null` or one of the eight category strings. Any other string, including a different case, is `422` |
| `project_name` | string or `null` |
| `project_number` | string or `null` |
| `due_date` | `null` or `YYYY-MM-DD`. Any other shape, or an impossible date, is `422` |
| `responsible_party` | string or `null` |
| `reference_number` | string or `null` |

For each key whose new value differs from the stored value, insert one correction row with the original value, the corrected value, and `created_at`. Then set `user_corrected` true. If `category` changed, set `category_source` to `user` and `category_confidence` to `1.0`, including when the user set `category` to null. If `category` was omitted or the category value is unchanged, leave source and confidence as they were.

If every supplied value already matches the stored value, write no log row, do not change `user_corrected`, and do not re-score. The response is still `200`.

When at least one value changes, re-score in that same transaction with the same pure function and `now` of `utcnow()`, and write the four priority columns. Then commit. The response is `200` and the detail object, the same shape as `GET /messages/{id}`, including the new classification keys and the updated priority keys. If the scorer raises, roll back. The correction is not saved with a stale score. This is different from sync, where a scorer bug must not drop the message text.

The route does not call Graph, Gmail, or `get_llm()`. It does not log the subject, the body, or the corrected text.

A later upsert skips every field that has a log row for that message. A field the user did not send can still be refreshed by the rule classifier. The scorer then uses the mix: locked category or due date, plus freshly extracted values for the rest.

The log is the labeled training set for a later model. This phase does not add a route that lists it. Tests read the table.

## Edge cases

Null or blank subject, preview, and body: keyword signals do not fire, and no category rule matches. The classifier result is category null, confidence `0.0`, source `rule` once the worker writes it, and null extracted fields. Unread and recency still can. The scorer still returns a score and a band.

Null `from_name` and `from_email`: sender signals do not fire, and `responsible_party` is null. Null is not a VIP and not a project role.

Null `is_read`: the unread signal does not fire. Sync already keeps a missing Graph `isRead` as null rather than false. A Gmail payload with no `labelIds` is stored the same way.

HTML leftovers: storage already runs `to_plain_text`, which drops tags and removes `<` and `>`. The scorer still strips leftover tags so a later writer cannot make `div` or `span` look like a keyword. `Please see the <b>RFI</b>` still counts as RFI. The classifier uses that same normalized text for category rules.

Very long bodies: `body_text` is already capped at 100000 characters. The classifier and the scorer read 8000 of those. A keyword or a date only past that cut does not fire. The same keyword in the subject still fires.

Non-English text: the phrase list is English. Other languages still get unread and recency when those inputs say so. This phase does not detect language and does not translate.

Duplicates: the same provider id on one mailbox is one row. A second upsert replaces the unlocked classification columns and the priority columns. The same provider id on two mailboxes is two rows, each classified from its own stored columns.

Messy provider items that lack an id or a parseable received time are dropped by the mapper and never reach the classifier. An address with no `@`, or with whitespace, is stored as a null `from_email`.

Relative days use the calendar date of `received_at` in `SCORING_TIMEZONE` (default `America/New_York`). A naive timestamp is UTC, then converted into that zone. `due by Friday` is that zone's Friday, including a Friday evening that is already Saturday in UTC, and it is not a date when `received_at` is missing. `last Friday` is the previous Friday. `next Friday` on a Friday is seven days ahead. A bare `by today`, including `drop by today`, is not a due date. `by <day>` counts only after a deadline word such as due, deadline, respond, response, needed, required, confirm, reply, submit, or return. `keyword_deadline` fires from `due by`, `response needed`, and `response due`. A month and day with no year, including `11/16` and `November 16`, is the nearest date from 30 days before the received date through 366 days after it. `1/5` received on December 30 is next year. `12/30` received on January 3 stays in the year that just ended. `Response Due Monday 11/16` keeps the numeric date. Project numbers match `Project #24-118` and `Project number:24-118` with no space after the mark. Reference numbers match `RFI-052` and `RFI No. 52`. Two category groups in one message keep the earlier group in the precedence list and set confidence `0.5`. Due and overdue compare the stored due date with the scoring zone's date of `now`, not the UTC date.

Shared signals, in addition to the construction keywords. Category points stay. The API still returns every reason. The full list is not cut to the top two or three.

| Code | Label | Weight | Fires when |
| ---- | ----- | ------ | ---------- |
| `asks_you` | Asks something of you | 20 | `please send`, `can you confirm`, `please confirm`, `sign and return`, `please reply`, `please advise`, `action required`, or a `?` in the subject. Not `let me know`, `could you`, or `would you`. A signature or a quoted reply is ignored |
| `repeat_ask` | Repeat ask | 15 | `final notice`, `second request`, `third request`, `following up`, or `second notice`. Not a bare `follow up`. The same signature and quote cut applies |
| `happening_soon` | Happening today or tomorrow | 15 | `meeting`, `showing`, `pickup`, or `inspection` within 48 characters of a date that is today or tomorrow in the scoring zone. Meeting minutes do not count the word meeting. A deadline word between the event and the date does not count |
| `act_on_attachment` | Attachment to act on | 8 | `sign and return`, `please sign`, `document to sign`, `form to sign`, `for your signature`, or `attached for signature` |
| `bulk_automated` | Bulk or automated | -40 | The sender local part is `no-reply`, `noreply`, `do-not-reply`, or `mailer-daemon`, and the construction exception below does not apply |
| `bulk_automated` | Bulk or automated | -20 | `unsubscribe`, `newsletter`, `view in browser`, `you are receiving this`, `your receipt`, `this is an automated`, or `do not reply`, the -40 sender rule did not fire, and the construction exception below does not apply |

`asks_you` is 20 so an RFI or change-order keyword (22) stays above a direct ask. A direct ask plus recent plus unread is 40, the medium line. Safety is also 20. Text after a sign-off line (`thanks`, `thank you`, `regards`, `best regards`, `sincerely`, `cheers`, `best`), a `--` delimiter, `Sent from my iPhone`, `Get Outlook for`, `-----Original Message-----`, `On ... wrote:`, or a line that starts with `>` is ignored for `asks_you` and `repeat_ask`.

Neither bulk penalty fires when the message is an RFI, change order, submittal, or safety item. That is a category in that set, a reference that starts with `RFI`, `CO`, `PCO`, or `Submittal`, or one of those phrases in the text. It also does not fire when the sender domain is `procore.com`, `autodesk.com`, `plangrid.com`, `buildertrend.com`, `bluebeam.com`, `aconex.com`, or `oraclecloud.com`, including a subdomain. Those are the construction platforms that send real notices from no-reply addresses. An invoice from any other no-reply address still takes the penalty.

`SCORING_TIMEZONE` is optional. The default is `America/New_York`. An unknown value uses that same default. The Docker image installs `tzdata` because `python:3.12-slim` has no system zoneinfo.

A negative weight can pull the sum down. The stored score is still clamped to 0 through 100. `waiting on your reply`, CC-only, and "someone you have replied to" are not signals yet: this sync does not store thread direction, recipients, or sent mail. The VIP tuple is still the known-sender list, and it still ships empty.

A user correction to `meeting_notes` removes the category reason and leaves keyword reasons in place, because those still match the text. The score can stay high after that correction. That is the contract until the founders change it. It is also an open question below.

## Test plan

Use fixture dicts and a fixed `now` of `2026-11-09T18:00:00Z`. Do not call Graph, Gmail, `urlopen`, or `get_llm`. The pure functions need no database. Patch `app.services.llm.factory.get_llm` so a call fails the test. The re-score job and the pure functions also fail the test if they call `app.worker.tasks._graph_get` or the Gmail HTTP helper.

`sync_mailbox_delta` may still call the existing provider fakes. It must not call `get_llm`.

### Scorer, with category and due date left null

These are the original priority fixtures. Expected results at that `now`:

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

Cap note. Unread, `received_at` two hours before `now`, `from_name` `Project Manager`, subject `URGENT RFI and change order`, body `Submittal attached. Stop work until you respond by Friday. Lien and pay app are past due. There is a deadline.` Category and due date are null in this row. Signals sum to 150. Stored score is 100.0. Reason order: `keyword_change_order` 22, `keyword_rfi` 22, `keyword_safety` 20, `keyword_deadline` 16, `keyword_urgent` 16, `keyword_submittal` 14, `keyword_payment` 12, `recent` 12, `sender_project_role` 8, `unread` 8.

Also cover:

- The same phrase in the subject and the body produces one reason.
- `architecture` does not match `architect`. `pm@mail.vip.example` does not match VIP domain `vip.example`.
- Body keyword only past character 8000 does not fire. The same keyword in the subject does.
- Leftover HTML `<b>RFI</b>` fires `keyword_rfi` and does not invent a keyword from the tag name.
- A passed-in `category` of `rfi` and a null due date adds `category_rfi` 10 and does not add a due-date reason.
- A passed-in `due_date` of `2026-11-11` adds `due_soon` 14. `2026-11-08` adds `due_overdue` 25. `2026-11-14` adds `due_within_7_days` 8. `2026-12-01` adds none.
- `meeting_notes` and `other` add no category reason.

### Classifier

Same `now` is unused. Assert the category, the confidence, and the extracted fields.

| Case | Inputs | Category | Confidence | Extracted |
| ---- | ------ | -------- | ---------- | --------- |
| RFI number | Subject `RFI #042 door hardware` | `rfi` | 1.0 | reference `RFI 042` |
| Change order | Subject `CO #18` | `change_order` | 1.0 | reference `CO 18` |
| Change order words | Subject `Change order 18` | `change_order` | 1.0 | reference `CO 18` |
| Submittal, no number | Subject `Submittal for curtain wall` | `submittal` | 1.0 | reference null |
| Pay application | Subject `Pay App 12` | `invoice` | 1.0 | reference `Pay App 12` |
| Invoice | Subject `Invoice 441` | `invoice` | 1.0 | reference `Invoice 441` |
| Safety | Subject `Near miss at the loading dock` | `safety` | 1.0 | reference null |
| Schedule | Subject `Project schedule update` | `schedule` | 1.0 | reference null |
| Minutes | Subject `Meeting minutes for the site walk` | `meeting_notes` | 1.0 | reference null |
| No rule | Subject `Hello from the trailer` | null | 0.0 | all extracted null |
| Two groups | Subject `RFI 7 and submittal 3` | `rfi` | 0.5 | reference `RFI 7` |
| Safety wins | Subject `Stop work on RFI 042` | `safety` | 0.5 | reference null |
| Bare co | Subject `Acme Co delivery` | null | 0.0 | reference null |
| Weekday | Subject `RFI 12 due by Friday` | `rfi` | 1.0 | reference `RFI 12`, due date null |
| Calendar date | Subject `RFI 042`. Body `Project name: Harbor School`. Next line `Project # 24-118`. Next line `Due date 2026-11-11`. From `Alex Rivera, Superintendent` | `rfi` | 1.0 | name `Harbor School`, number `24-118`, due `2026-11-11`, party `Superintendent`, reference `RFI 042` |
| Past the cut | Due date only past character 8000 of `body_text`, subject `Hello`, preview null | null | 0.0 | due date null |
| Preview only | Subject `Hello`, body null, preview `Due date 2026-11-11` | null | 0.0 | due date `2026-11-11` |

`RFIs` matches `rfi`. `architecture` does not match `architect`.

### Worker, after the classifier has run

The worker classifies, then scores. On the original texts, with no correction:

| Case | Score | Band | Reasons added on top of the null-category row |
| ---- | ----- | ---- | --------------------------------------------- |
| Change order text | 92.0 | `high` | `category_change_order` 10. `due by Friday` on Monday `2026-11-09` is `2026-11-13`, so `due_within_7_days` 8 is also present. Reference `CO 18` |
| RFI text (`RFI 12`, sketch body) | 52.0 | `medium` | `category_rfi` 10. Reference `RFI 12` |
| Old invoice text | 18.0 | `low` | `category_invoice` 6. Reference `Invoice 441` |
| Submittal this week | 28.0 | `low` | `category_submittal` 8. Reference null |
| Cap text | 100.0 | `high` | Category is `safety` at confidence `0.5` (safety, change order, RFI, submittal, and invoice all match). Add `category_safety` 12. `respond by Friday` on Monday `2026-11-09` sets due date `2026-11-13`. Reason order inserts `category_safety` 12 before `keyword_payment` 12 and `recent` 12 |

An RFI with a far due date scores lower than the same RFI with a near due date. Read both as unread, `received_at` `2026-11-09T16:00:00Z`, subject `RFI 042 door hardware`:

| Body | Due reason | Score | Band |
| ---- | ---------- | ----- | ---- |
| `Due date 2026-12-01.` | none | 68.0 | `medium` |
| `Due date 2026-11-11.` | `due_soon` 14 | 82.0 | `high` |

Both also have `keyword_rfi` 22, `keyword_deadline` 16, `category_rfi` 10, `recent` 12, and `unread` 8. The near date is the only difference. Order for the near date: `keyword_rfi` 22, `keyword_deadline` 16, `due_soon` 14, `recent` 12, `category_rfi` 10, `unread` 8.

Old read invoice, subject `Invoice 100`, body `Due by 2026-10-01.`, `received_at` `2026-10-01T12:00:00Z`: score 59.0, band `medium`, reasons `due_overdue` 25, `keyword_deadline` 16, `keyword_payment` 12, `category_invoice` 6.

Also cover:

- Upserting the same `(connected_account_id, graph_id)` twice leaves one row and replaces the unlocked classification columns and the priority columns. This applies to a Microsoft item and to a Google item.
- Existing fixture `GRAPH_ITEM` in `tests/test_mailbox_reliability.py`, after `graph_message_fields`, at this `now`: subject becomes `Invoice 12`, body `Hello`, unread, received `2024-06-01`. Classifier: `invoice`, confidence `1.0`, reference `Invoice 12`. Combined score 26.0, band `low`, reasons `keyword_payment` 12, `unread` 8, `category_invoice` 6. The null-category scorer result for that text is still 20.0 with `keyword_payment` then `unread`. `test_sync_success_failure_and_superseded_generation` does not assert priority or classification fields today and has to stay green. PR 3 adds the combined assertion. It keeps using a fake `_graph_get`.
- A classifier exception leaves message text saved, leaves classification columns unchanged, still scores from the stored category and due date, and still lets a successful provider sync finish idle.
- A scorer exception leaves message text saved, leaves priority columns unchanged, and still lets a successful provider sync finish idle.
- Re-score updates classification and priority for one connected mailbox and does not call Graph, Gmail, or `get_llm`.
- A disconnected account is skipped.

API tests for the mapping, with columns written directly. PR 2:

- Null score, null band, null reasons column, null classification columns: priority `null`, `null`, `[]`, and the nine classification keys at null except `user_corrected` `false`.
- Score 50 and null band and null reasons: `50.0`, `null`, `[]`, and the existing priority sort order stays (higher score first, equal scores newer first, null scores last).
- Score 74.0, band `high`, and the change-order reason list: the JSON objects match the contract, and each reason object still has exactly `code`, `label`, and `weight`.
- A stored category `rfi`, source `rule`, confidence `1.0`, due date `2026-11-11`, and the other extracted fields: the JSON matches the classification table, and `due_date` is the string `2026-11-11`.
- `set(item)` equals `LIST_KEYS` after `LIST_KEYS` gains the nine classification keys. Detail is that set plus `body_text` and `draft`.

PR 4:

- A category change writes one log row with the original value, the corrected value, and a `created_at`. Three changed fields write three rows.
- The response is the detail object. `category_source` is `user`, `category_confidence` is `1.0`, and `user_corrected` is `true`.
- Correcting the old invoice fixture's category from `invoice` to `rfi` re-scores in the request. `keyword_payment` remains. `category_invoice` is gone. `category_rfi` is present.
- A later upsert does not overwrite a corrected category or a corrected due date. An extracted field with no log row can change.
- Sending the stored value writes no log row.
- `{}`, an unknown key, a category of `RFI`, and a due date of `Friday` are `422`.
- Another user, a disconnected mailbox, and an unknown id are `404` with detail `Message not found.`
- The handler does not call Graph, Gmail, or `get_llm`.

## Implementation split

James writes these three PRs. This spec is none of them.

### PR 2: schema and API mapping

- Alembic revision as described under Data model, including the classification columns, plus the `Message` fields.
- `_message_response` reads `priority_band`, `priority_reasons`, and the classification columns.
- `LIST_KEYS` and the detail key set grow by the nine classification keys.
- No scorer, no classifier, no worker change, no correction route, no call to `get_llm`, no environment or config edit.

Acceptance:

- Upgrade and downgrade succeed. Existing message rows keep a null score, a null band, a null reasons column, and null classification columns.
- List and detail match the API rows in the test plan above.
- `sort=priority` is unchanged.
- `priority_reasons` is an array. `user_corrected` is a boolean. `priority_scored_at` is absent.
- `test_message_list_contract_sort_and_total` passes without weakening its null-band assertion.
- `PATCH /messages/{id}/classification` is not registered.

### PR 3: worker, rule scorer, rule classifier, and tests

- The pure scorer, the pure classifier, and the weights above.
- Call them from both provider loops of `sync_mailbox_delta` after each successful upsert, in that loop's transaction.
- `rescore_mailbox_priority` registered and not scheduled. It classifies and scores.
- Tests in the test plan for the scorer, the classifier, and the worker. No correction route yet, so no field is locked. No new environment variable. `LLM_PROVIDER` stays `stub`.

Acceptance:

- The null-category fixture table matches, including the cap at 100.0 and the empty VIP tuple.
- The classifier table matches, including confidence `0.5` and a null due date for `Friday`.
- The combined worker table matches, including the near due date scoring higher than the far due date.
- A second upsert does not create a duplicate message, for Microsoft or for Google.
- A classifier exception and a scorer exception leave message text saved and still let a successful provider sync finish idle.
- Re-score updates one connected mailbox and does not call Graph, Gmail, or `get_llm`.
- A disconnected account is skipped.
- Sync tests still fake the provider calls. Neither function nor the re-score job opens a socket.

### PR 4: correction endpoint and its log

- The `message_classification_corrections` revision.
- `PATCH /messages/{message_id}/classification` as specified.
- The upsert and the re-score job skip fields that have a log row.
- Tests in the PR 4 block of the test plan.

Acceptance:

- A change stores the original value, the corrected value, and a timestamp.
- The detail response shows `user` / `1.0` / `user_corrected: true` when the category changed, and the priority columns match the corrected inputs.
- A later sync keeps the corrected fields and can refresh an uncorrected one.
- Isolation matches the message detail route: `404` with `Message not found.`
- No provider call and no `get_llm()` call.

## Later phases

These are not PR 2, PR 3, or PR 4.

LLM classifier. After DEC-021 is Decided, and after zero-data retention is confirmed for the org that would receive the mail, a later change can add a hosted classifier behind a new flag, `CLASSIFICATION_PROVIDER`. The default is `rules`. `rules` is the path this phase specifies, and it stays the path that production uses until someone turns the flag. The flag does not exist in this phase, so PR 2, PR 3, and PR 4 do not add an environment variable and do not branch on `LLM_PROVIDER`. `get_llm()` stays the form-prefill seam. Its stub return value is `[stub llm]`, which is not a category. The hosted classifier, when it exists, has to return the same category strings and the same extracted fields, and the rule result remains the fallback when the flag is `rules` or the call fails.

Attachment text. A later phase can read attachment text, for example a PDF on the message, and pass that text to the same classifier. Gmail already skips filename parts. Graph `$select` does not include attachments. This phase does not download them and does not store them.

Fine-tuned model. Optional, after the correction log has real rows from production mail. It waits on the same DEC-021 decision and on a separate cost decision. It is not the default. The rule classifier stays the default after a model exists. Training data is the correction table: original value, corrected value, timestamp, and the message text already stored on the row. This phase does not export that table and does not start a training job.

## Deferred until the live mailbox test

The live sync has not been checked against a real mailbox. Until that passes, leave these as follow-ups:

- Changing weights, band cutoffs, the keyword list, or the category phrases to match real construction mail.
- Putting real company domains in the VIP tuple.
- Assuming sender, body, or `is_read` are usually present. Tests keep null fixtures.
- Storing recipients and adding a direct-vs-CC signal. Confirm from real payloads whether the current `$select` is enough.
- Phrase lists for languages other than English.
- Any product decision about backfill beyond the manual `rescore_mailbox_priority` job. The job is the only backfill this phase specifies.

## Deferred until the ZDR decision

[DEC-021](https://app.notion.com/p/40f6b731e7548182814de37379a770b2) proposes Anthropic with org zero-data-retention: Claude Haiku 5.5 (`claude-haiku-5-5`) for sorting, Sonnet 5.5 for form prefill. Status is **Proposed**. It is not a decision to send mail to a model.

Until that status is Decided, and until ZDR is confirmed for the org that would receive customer mail:

- The rule classifier is the only classifier, and the rule scorer is the only scorer.
- Do not call `get_llm()`, and do not branch on `LLM_PROVIDER`.
- Do not add an Anthropic client, a prompt, or a request that includes subject or body.
- Do not treat `StubLLM` as a classifier or a scorer. Its return value is `[stub llm]`, not a category or a score.
- Do not add `CLASSIFICATION_PROVIDER`.

Form prefill stays out of phase 6 even after ZDR is decided.

## Open questions

The contract above is what James implements. These are the points the founders still confirm. The category list is **pending founder confirmation**.

1. Is the category list final: `submittal`, `rfi`, `change_order`, `invoice` (including pay applications), `meeting_notes`, `safety`, `schedule`, `other`? Should a pay application be its own category? Should no rule match be stored as `other` instead of null?
2. What should the frontend show, for Colby? Category, due date, reference number, confidence, and a corrected marker are all on list and detail in this contract. This spec does not add a category filter. It does not say which of those fields the list renders.
3. Category points add to keyword points. A user who corrects an RFI to `meeting_notes` drops the category reason and keeps `keyword_rfi` if the text still says RFI. Should a correction replace the overlapping keyword reason instead?
4. Due-date windows are overdue 25, within 3 days 14, and within 7 days 8. A weekday is resolved from `received_at`. Confirm those weights after a live mailbox.
5. `responsible_party` is a role on the From line. Should the rule also take a person's name from the body?
6. A correction locks only the fields the user sent. The next sync may refresh the others. Should any correction lock the whole classification?
7. The mailbox owner is the only person who can correct a message. Is a shared reviewer in scope later?
