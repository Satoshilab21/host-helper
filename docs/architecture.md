# Architecture

Host Helper is a scheduled Python CLI with SQLite state and Google API integrations. A single run synchronizes Calendar before Tasks so maintenance rules use the latest reconciled booking history.

## Data flow

1. `ics_loader` parses the Airbnb feed into bookings and blocked intervals.
2. `gmail_fetcher` reads confirmation messages; `email_parser` extracts reservation details and associates them with feed confirmation codes.
3. `block_matcher` matches changed blocked intervals to persisted state and filters new short near-term filler blocks.
4. `enrich_bookings` merges available email details, persists bookings, and reconciles booking lifecycle states.
5. `calendar_sync` upserts booking/block events, removes cancellations, and synchronizes trash reminders for the next 60 days.
6. `task_rules` evaluates booking history; `tasks_sync` reconciles Google Tasks and records derived task mappings.
7. `run` clears prior failure alerts after success and reports status to the optional heartbeat endpoint.

## Module responsibilities

| Modules | Responsibility |
| --- | --- |
| `cli`, `config` | Command parsing, explicit `.env` loading, configuration defaults, feed validation |
| `ics_loader`, `gmail_fetcher`, `email_parser` | External inputs and reservation enrichment |
| `block_matcher`, `enrich_bookings` | Identity matching and booking reconciliation |
| `db` | Schema, booking history, derived event/task mappings, rule state |
| `trash_rule`, `task_rules` | Scheduling calculations |
| `calendar_sync`, `tasks_sync` | Google API authentication and reconciliation |
| `run`, `authorize` | Orchestration, failure reporting, heartbeat, headless OAuth |
| `dry_run_*`, `deduplicate_cleaning_tasks` | Preview and recovery commands |

## Identity and repeat runs

Google Calendar accepts client-supplied IDs. Booking IDs are SHA-1 hashes of stable feed identifiers, while trash and failure events use stable synthetic keys. Repeated upserts address the same event. SHA-1 is used for identity, not for protecting secrets.

Google Tasks assigns its own IDs. Host Helper writes a stable key on the first line of each task's notes, lists all pages including completed/hidden tasks, and builds an index before changes. Existing tasks are updated only when their fields differ. When duplicates exist, a completed copy is preferred so cleanup preserves completion state. The coordinator commits canonical mappings before attempting at most 20 duplicate deletions per run; quota failures pause that cleanup batch.

The legacy `open_manager_key:` marker and `open_manager.db` filename are deliberate storage contracts. Renaming them during a branding change would disconnect the application from existing tasks or state. Default task-list migration changes the existing list's title while preserving its ID.

## State and scheduling

SQLite stores source bookings, derived events/tasks, and rule state. Booking lifecycle reconciliation retains historical stays for counter-based scheduling and removes cancelled future bookings from the active timeline. Email fields use sticky merging: a missing later email does not erase previously extracted details.

Checkout and guest-night counters are recomputed from the persisted timeline on every run. Air-filter cooldown state advances when a scheduled check's due date arrives, rather than when a future task is first created. This prevents daily sync from continually moving the reminder forward.

Blocked intervals need additional reconciliation because Airbnb can replace their identifiers when dates change. Rekeying a matched interval preserves its Google event mapping and associated local state.

## Failure behavior

An exception produces a nonzero exit status and full traceback in the process output. Host Helper also attempts a date-keyed Calendar failure alert; traceback text is HTML-escaped. A successful calendar-and-task run attempts to remove earlier alerts, verifying their generated IDs before deletion. Alert cleanup and heartbeat errors are reported without overriding sync status.

This is not a distributed transaction. Google writes and SQLite commits can succeed independently, and concurrent runs are unsupported. Stable identifiers allow a later run to recover many partial-write cases. API insert operations that lack caller-supplied IDs are not automatically retried, avoiding duplicate creation after an ambiguous response.

## Configuration and testing

The CLI loads `.env` from the working directory after command parsing and before importing service modules. Importing the package does not load deployment settings, create a database, or contact Google. Commands validate the feed only when they need it; task previews can run from local history.

Unit tests use fake Google API responses, synthetic messages, fixed dates where needed, and in-memory SQLite. CLI tests also start isolated subprocesses without configuration to verify help, imports, and error handling. CI adds a Python version matrix, lint/format checks, package builds, and a full-history secret scan. Live API integration still requires an operator's private deployment.
