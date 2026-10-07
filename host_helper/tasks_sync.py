import logging
import os.path
from dataclasses import dataclass
from datetime import date

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from . import db
from .config import TASK_LIST_TITLE
from .task_rules import evaluate_task_rules

SCOPES = ["https://www.googleapis.com/auth/tasks"]
AIR_FILTER_LAST_FIRED_KEY = "air_filter_last_fired"
CREDENTIALS_FILE = "credentials.json"  # same OAuth client as calendar_sync.py/gmail_fetcher.py
TOKEN_FILE = "token_tasks.json"  # separate token -- different scope, cached independently

API_RETRIES = 3
MAX_DUPLICATE_DELETIONS = 20
LOGGER = logging.getLogger(__name__)


def get_tasks_service():
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)

        with open(TOKEN_FILE, "w") as token:
            token.write(creds.to_json())

    return build("tasks", "v1", credentials=creds)


# --- Task list management ---
# Tasks live inside a task list (tasklist), identified by its own id -- unlike
# Calendar's single implicit "primary" calendar, we need to find-or-create the
# list we want to use before we can create any tasks in it.


def _find_task_list(service, title: str) -> dict | None:
    legacy = None
    page_token = None
    while True:
        response = (
            service.tasklists().list(maxResults=1000, pageToken=page_token).execute(num_retries=API_RETRIES)
        )
        for task_list in response.get("items", []):
            if task_list["title"] == title:
                return task_list
            if title == "Host Helper" and task_list["title"] == "Open Manager":
                legacy = task_list
        page_token = response.get("nextPageToken")
        if not page_token:
            return legacy


def find_task_list(service, title: str = TASK_LIST_TITLE) -> str | None:
    """Find the current list, accepting the original list during migration."""
    existing = _find_task_list(service, title)
    return existing["id"] if existing is not None else None


def get_or_create_task_list(service, title: str = TASK_LIST_TITLE) -> str:
    existing = _find_task_list(service, title)
    if existing is not None:
        if existing["title"] != title:
            service.tasklists().update(tasklist=existing["id"], body={"title": title}).execute(
                num_retries=API_RETRIES
            )
        return existing["id"]

    created = service.tasklists().insert(body={"title": title}).execute()
    return created["id"]


# --- Generic Tasks CRUD core ---
#
# Unlike Calendar's events().insert(), the Tasks API does NOT accept a
# client-supplied "id" -- Google always assigns its own, and an "id" passed
# in the insert body is ignored. That breaks the deterministic-hash-as-ID
# upsert pattern used everywhere in calendar_sync.py.
#
# Workaround: we control our own stable key (e.g. "trash-2026-07-23") and
# stash it as a recognizable prefix in the task's "notes" field. To upsert,
# we search existing tasks, including completed tasks, for one whose notes carry
# our key, and update that task if found, or create a new one if not. This
# means lookup must cover ALL pages of tasks. The rule coordinator reads the
# list once per sync and shares an index with each upsert, rather than making
# another full scan for every task (including all historical duplicates).

_KEY_PREFIX = "open_manager_key:"
# This persisted marker is a storage contract. Branding must not change it.


def _notes_with_key(key: str, notes: str | None) -> str:
    return f"{_KEY_PREFIX}{key}\n{notes or ''}".rstrip()


def list_tasks(service, tasklist_id: str) -> list[dict]:
    """Read the entire list before making changes that could shift its pages."""
    tasks = []
    seen_ids = set()
    page_token = None
    while True:
        response = (
            service.tasks()
            .list(
                tasklist=tasklist_id,
                maxResults=100,
                pageToken=page_token,
                showCompleted=True,
                showHidden=True,
            )
            .execute(num_retries=API_RETRIES)
        )
        for task in response.get("items", []):
            if not task.get("deleted") and task["id"] not in seen_ids:
                tasks.append(task)
                seen_ids.add(task["id"])
        page_token = response.get("nextPageToken")
        if not page_token:
            return tasks


def task_key(task: dict) -> str | None:
    """Recognize only the complete marker line written by Host Helper."""
    lines = (task.get("notes") or "").splitlines()
    if lines and lines[0].startswith(_KEY_PREFIX):
        return lines[0][len(_KEY_PREFIX) :] or None
    return None


def _find_tasks_by_key(service, tasklist_id: str, key: str) -> list[dict]:
    matches = [task for task in list_tasks(service, tasklist_id) if task_key(task) == key]
    # Keep a completed copy if present so duplicate cleanup preserves completion.
    return sorted(matches, key=lambda task: task.get("status") != "completed")


def _index_tasks(tasks: list[dict]) -> dict[str, list[dict]]:
    index = {}
    for task in tasks:
        key = task_key(task)
        if key is not None:
            index.setdefault(key, []).append(task)
    return index


def _find_task_by_key(service, tasklist_id: str, key: str) -> dict | None:
    matches = _find_tasks_by_key(service, tasklist_id, key)
    return matches[0] if matches else None


def create_task(service, tasklist_id: str, key: str, body: dict) -> dict:
    body = {**body, "notes": _notes_with_key(key, body.get("notes"))}
    return service.tasks().insert(tasklist=tasklist_id, body=body).execute()


def update_task(service, tasklist_id: str, google_task_id: str, body: dict) -> dict:
    return (
        service.tasks()
        .update(tasklist=tasklist_id, task=google_task_id, body=body)
        .execute(num_retries=API_RETRIES)
    )


def delete_task(service, tasklist_id: str, google_task_id: str) -> None:
    # "Already gone" is success -- 404 (never existed) and 410 Gone (existed,
    # already deleted) both mean there is nothing left to remove.
    try:
        service.tasks().delete(tasklist=tasklist_id, task=google_task_id).execute(num_retries=API_RETRIES)
    except HttpError as error:
        if error.resp.status not in (404, 410):
            raise


@dataclass
class DuplicateCleanupState:
    remaining: int = MAX_DUPLICATE_DELETIONS
    paused: bool = False


def _cleanup_duplicates(
    service,
    tasklist_id: str,
    key: str,
    matches: list[dict],
    state: DuplicateCleanupState,
) -> list[dict]:
    remaining = matches[:1]
    for duplicate in matches[1:]:
        if state.paused or state.remaining == 0:
            remaining.append(duplicate)
            continue
        state.remaining -= 1
        try:
            delete_task(service, tasklist_id, duplicate["id"])
        except HttpError as error:
            remaining.append(duplicate)
            # Quota failures may last until a quota window resets. Stop this
            # cleanup batch instead of hammering Google with more deletions.
            if error.resp.status in (403, 429):
                state.paused = True
            LOGGER.warning(
                "Duplicate cleanup incomplete for %s: kept task %s; could not delete %s "
                "(HTTP %s). Will retry on the next sync. Google error: %s",
                key,
                matches[0]["id"],
                duplicate["id"],
                error.resp.status,
                error,
            )
    return remaining


def _task_needs_update(existing: dict, body: dict) -> bool:
    for field, value in body.items():
        if field == "id":
            continue
        if field == "due":
            if (existing.get(field) or "")[:10] != value[:10]:
                return True
        elif existing.get(field) != value:
            return True
    return False


def upsert_task(
    service,
    tasklist_id: str,
    key: str,
    body: dict,
    *,
    task_index: dict[str, list[dict]] | None = None,
    cleanup_duplicates: bool = True,
) -> dict:
    if task_index is None:
        matches = _find_tasks_by_key(service, tasklist_id, key)
    else:
        matches = sorted(task_index.get(key, []), key=lambda task: task.get("status") != "completed")
    if matches:
        existing = matches[0]
        body = {**body, "id": existing["id"], "notes": _notes_with_key(key, body.get("notes"))}
        if existing.get("status") == "completed":
            body["status"] = "completed"
            if existing.get("completed"):
                body["completed"] = existing["completed"]
        result = existing
        if _task_needs_update(existing, body):
            result = update_task(service, tasklist_id, existing["id"], body)
        remaining = [result, *matches[1:]]
        if cleanup_duplicates:
            remaining = _cleanup_duplicates(service, tasklist_id, key, remaining, DuplicateCleanupState())
        if task_index is not None:
            task_index[key] = remaining
        return result
    # Inserts are not idempotent: retrying an ambiguous insert could itself
    # create duplicates. A later run can rediscover it by its notes marker.
    result = create_task(service, tasklist_id, key, body)
    if task_index is not None:
        task_index[key] = [result]
    return result


def complete_task(service, tasklist_id: str, key: str) -> dict | None:
    existing = _find_task_by_key(service, tasklist_id, key)
    if existing is None:
        return None
    existing["status"] = "completed"
    return update_task(service, tasklist_id, existing["id"], existing)


# --- Rule coordinator ---


def _due_rfc3339(due: date) -> str:
    # Tasks API 'due' is an RFC3339 timestamp but only the date part is honored.
    return f"{due.isoformat()}T00:00:00.000Z"


def sync_task_rules(service, tasklist_id: str) -> list:
    """Evaluate all Google Tasks rules against booking history and sync the
    resulting task set: upsert each, record in derived_events (sink='tasks'),
    delete any previously-synced task no longer produced, and advance the
    air-filter cooldown state. Duplicate cleanup runs in bounded batches after
    those changes commit; HTTP cleanup failures are logged and retried on later
    runs. Returns the TaskItems that were synced."""
    db.init_db()
    today = date.today()
    with db.get_connection() as conn:
        history = db.get_timeline_events(conn)

        last_fired_str = db.get_rule_state(conn, AIR_FILTER_LAST_FIRED_KEY)
        last_fired = date.fromisoformat(last_fired_str) if last_fired_str else None

        items = evaluate_task_rules(history, last_fired)
        task_index = _index_tasks(list_tasks(service, tasklist_id)) if items else {}

        newest_air_filter = last_fired
        for item in items:
            body = {"title": item.title, "due": _due_rfc3339(item.due_date)}
            result = upsert_task(
                service,
                tasklist_id,
                item.key,
                body,
                task_index=task_index,
                cleanup_duplicates=False,
            )
            db.upsert_derived_event(
                conn,
                key=item.key,
                rule_name=item.rule_name,
                sink="tasks",
                due_date=item.due_date.isoformat(),
                google_task_id=result.get("id"),
            )

            if item.rule_name == "air_filter":
                # Only treat a scheduled check as "fired" once its due date has
                # actually passed. Advancing on the day it is SCHEDULED would
                # make the next run use that future date as its cooldown
                # baseline and push the task another 30 days out -- with a daily
                # cron the task would march into the future forever and never
                # come due.
                if item.due_date <= today and (
                    newest_air_filter is None or item.due_date > newest_air_filter
                ):
                    newest_air_filter = item.due_date

        # Advance the air-filter cooldown only for checks whose day has arrived.
        if newest_air_filter and newest_air_filter != last_fired:
            db.set_rule_state(conn, AIR_FILTER_LAST_FIRED_KEY, newest_air_filter.isoformat())

        # Delete Google Tasks for any rule-task no longer produced. Scope the
        # stale check per rule_name so one rule's keys don't look "missing" to
        # another's. Rule names are derived from the current run's items PLUS
        # whatever rule_names already have rows in derived_events, so a rule
        # that produces zero tasks this run (e.g. its last task just got
        # deleted) still gets its stale check run instead of being skipped.
        known_rule_names = {i.rule_name for i in items} | {
            r["rule_name"] for r in db.get_active_derived_events(conn) if r["sink"] == "tasks"
        }
        for rule_name in known_rule_names:
            rule_present = [i.key for i in items if i.rule_name == rule_name]
            for stale in db.mark_missing_derived_stale(conn, rule_name, rule_present):
                if stale["google_task_id"]:
                    delete_task(service, tasklist_id, stale["google_task_id"])
                db.delete_derived_event(conn, stale["key"])

    # Commit canonical task mappings before spending quota on optional cleanup.
    # A large backlog is cleared across daily runs, with a single shared budget.
    cleanup_state = DuplicateCleanupState()
    present_keys = dict.fromkeys(item.key for item in items)
    for key in present_keys:
        task_index[key] = _cleanup_duplicates(service, tasklist_id, key, task_index[key], cleanup_state)
    pending = sum(max(0, len(task_index[key]) - 1) for key in present_keys)
    if pending:
        LOGGER.warning("Task sync finished; %s duplicate task(s) remain for cleanup on later runs.", pending)

    return items


def sync_tasks() -> list:
    """Full task-side sync: authenticate, find/create the task list, and run
    all task rules against booking history. Uses the Tasks service only."""
    service = get_tasks_service()
    tasklist_id = get_or_create_task_list(service)
    return sync_task_rules(service, tasklist_id)


if __name__ == "__main__":
    synced = sync_tasks()
    for item in synced:
        print(f"{item.rule_name}: {item.title} (due {item.due_date})")
