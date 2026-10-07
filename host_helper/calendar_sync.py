import hashlib
import html
import os.path
from datetime import date, datetime, time, timedelta

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from . import db
from .config import (
    BLOCKED_COLOR_ID,
    BOOKING_COLOR_ID,
    CHECKIN_TIME,
    CHECKOUT_TIME,
    FAILURE_COLOR_ID,
    FAILURE_EVENT_TITLE,
    TIMEZONE,
    TRASH_COLOR_ID,
    TRASH_REMINDER_MINUTES,
)
from .enrich_bookings import fetch_enriched_bookings
from .ics_loader import Booking
from .trash_rule import TrashTask, evaluate_trash_rule

SCOPES = ["https://www.googleapis.com/auth/calendar.events"]
CALENDAR_ID = "primary"


def _combine(day: date, hhmm: str) -> str:
    hour, minute = (int(part) for part in hhmm.split(":"))
    return datetime.combine(day, time(hour=hour, minute=minute)).isoformat()


def _parse_airbnb_time(raw: str) -> time | None:
    # Airbnb writes times like "4:00 PM" in confirmation emails (Booking.checkin_time /
    # checkout_time) -- different format from config's "HH:MM" 24-hour default.
    try:
        return datetime.strptime(raw.strip(), "%I:%M %p").time()
    except ValueError:
        return None


def checkin_datetime(check_in_date: date, raw_time: str | None = None) -> str:
    if raw_time:
        parsed = _parse_airbnb_time(raw_time)
        if parsed:
            return datetime.combine(check_in_date, parsed).isoformat()
    return _combine(check_in_date, CHECKIN_TIME)


def checkout_datetime(check_out_date: date, raw_time: str | None = None) -> str:
    if raw_time:
        parsed = _parse_airbnb_time(raw_time)
        if parsed:
            return datetime.combine(check_out_date, parsed).isoformat()
    return _combine(check_out_date, CHECKOUT_TIME)


def google_event_id(ics_uid: str) -> str:
    return hashlib.sha1(ics_uid.encode()).hexdigest()


def popup_reminder(minutes_before: int) -> dict:
    return {
        "useDefault": False,
        "overrides": [{"method": "popup", "minutes": minutes_before}],
    }


def get_service():
    creds = None
    # The file token.json stores the user's access and refresh tokens, and is
    # created automatically when the authorization flow completes for the first
    # time.
    if os.path.exists("token.json"):
        creds = Credentials.from_authorized_user_file("token.json", SCOPES)
    # If there are no (valid) credentials available, let the user log in.
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
            creds = flow.run_local_server(port=0)
        # Save the credentials for the next run
        with open("token.json", "w") as token:
            token.write(creds.to_json())

    return build("calendar", "v3", credentials=creds)


# --- Generic Google Calendar CRUD core, shared by every event type. ---
# Each event type (bookings, trash tasks, future rules) only needs to supply
# a stable event_id and a body dict -- the actual insert/update/delete/upsert
# plumbing lives here once.


def create_calendar_event(service, event_id: str, body: dict) -> dict:
    return service.events().insert(calendarId=CALENDAR_ID, body={**body, "id": event_id}).execute()


def update_calendar_event(service, event_id: str, body: dict) -> dict:
    return service.events().update(calendarId=CALENDAR_ID, eventId=event_id, body=body).execute()


def delete_calendar_event(service, event_id: str) -> None:
    # "Already gone" is success. Google returns 404 when the id was never
    # created and 410 Gone when it existed but was already deleted -- deleting
    # a stale event twice (e.g. the same derived event dropped on two runs)
    # must not abort the sync.
    try:
        service.events().delete(calendarId=CALENDAR_ID, eventId=event_id).execute()
    except HttpError as error:
        if error.resp.status not in (404, 410):
            raise


def upsert_calendar_event(service, event_id: str, body: dict) -> dict:
    # 404 = never existed, 410 = existed and was deleted. Both mean "create it",
    # so a previously-deleted event (e.g. one the user removed by hand, or a
    # same-day failure event) can be recreated rather than crashing the run.
    try:
        return update_calendar_event(service, event_id, body)
    except HttpError as error:
        if error.resp.status not in (404, 410):
            raise
        return create_calendar_event(service, event_id, body)


# --- Booking events ---


def build_event_body(booking: Booking) -> dict:
    # Prefer the id already stored for this booking. For blocks that Airbnb
    # reissued under a new uid, that stored id is the ORIGINAL one, so the
    # existing calendar event is updated in place rather than duplicated.
    event_id = booking.google_event_id or google_event_id(booking.uid)
    title = booking.guest_name or booking.summary or "Unknown summary"
    return {
        "id": event_id,
        "summary": (f"{title}" + (f" ({booking.confirmation_code})" if booking.confirmation_code else "")),
        "start": {
            "dateTime": checkin_datetime(booking.check_in, booking.checkin_time),
            "timeZone": TIMEZONE,
        },
        "end": {
            "dateTime": checkout_datetime(booking.check_out, booking.checkout_time),
            "timeZone": TIMEZONE,
        },
        "reminders": {"useDefault": False},
        "colorId": BOOKING_COLOR_ID,
    }


def upsert_booking(service, booking: Booking) -> dict:
    body = build_event_body(booking)
    result = upsert_calendar_event(service, body["id"], body)
    with db.get_connection() as conn:
        db.set_event_google_id(conn, booking.uid, body["id"])
    return result


# --- Blocked/unavailable date events ---


def build_blocked_event_body(booking: Booking) -> dict:
    # All-day event: no real check-in/check-out time applies to a block (no
    # guest, no enrichment), so use Google's date-only start/end instead of
    # the timed dateTime/timeZone shape real bookings use.
    return {
        # See build_event_body: reuse the stored id so a block whose uid changed
        # keeps its existing calendar event instead of churning.
        "id": booking.google_event_id or google_event_id(booking.uid),
        "summary": "Family",
        "start": {"date": booking.check_in.isoformat()},
        "end": {"date": booking.check_out.isoformat()},
        "reminders": {"useDefault": False},
        "colorId": BLOCKED_COLOR_ID,
    }


def upsert_blocked_date(service, booking: Booking) -> dict:
    body = build_blocked_event_body(booking)
    result = upsert_calendar_event(service, body["id"], body)
    with db.get_connection() as conn:
        db.set_event_google_id(conn, booking.uid, body["id"])
    return result


def cleanup_cancelled_events(service) -> int:
    """Delete Google Calendar events for bookings the DB has marked 'cancelled'
    (removed from the .ics before check-in). Completed bookings are left alone.
    Recomputes the event id from the uid (deterministic), so this works even if
    google_event_id was never persisted. Returns how many were deleted."""
    deleted = 0
    with db.get_connection() as conn:
        for row in db.get_cancelled_events_needing_cleanup(conn):
            event_id = row["google_event_id"] or google_event_id(row["uid"])
            delete_calendar_event(service, event_id)
            db.clear_event_google_id(conn, row["uid"])
            deleted += 1
    return deleted


# --- Trash-rule derived events ---


def trash_task_key(task: TrashTask) -> str:
    return f"trash-{task.pickup_date.isoformat()}"


def build_trash_event_body(task: TrashTask) -> dict:
    return {
        "id": google_event_id(trash_task_key(task)),
        "summary": "Put trash cans out",
        "start": {"dateTime": task.put_out_at, "timeZone": TIMEZONE},
        "end": {"dateTime": task.put_out_at, "timeZone": TIMEZONE},
        "reminders": popup_reminder(TRASH_REMINDER_MINUTES),
        "colorId": TRASH_COLOR_ID,
    }


def upsert_trash_task(service, task: TrashTask) -> dict:
    body = build_trash_event_body(task)
    return upsert_calendar_event(service, body["id"], body)


def sync_trash_tasks(service, tasks: list[TrashTask]) -> None:
    """Upsert the current set of trash tasks AND remove any previously-synced
    trash event that's no longer in this run's output (e.g. a booking got
    cancelled, so a trash day that used to fire no longer should). This closes
    the trash rule's stale-event gap using the derived_events table."""
    db.init_db()
    with db.get_connection() as conn:
        present_keys = []
        for task in tasks:
            key = trash_task_key(task)
            body = build_trash_event_body(task)
            upsert_calendar_event(service, body["id"], body)
            db.upsert_derived_event(
                conn,
                key=key,
                rule_name="trash",
                sink="calendar",
                due_date=task.pickup_date.isoformat(),
                google_event_id=body["id"],
            )
            present_keys.append(key)

        # Delete Google events for trash days that are no longer computed.
        for stale in db.mark_missing_derived_stale(conn, "trash", present_keys):
            if stale["google_event_id"]:
                delete_calendar_event(service, stale["google_event_id"])
            db.delete_derived_event(conn, stale["key"])


def _failure_description(error_text: str, error: Exception | None = None) -> str:
    """Put the actionable error first and escape exception text for Calendar HTML."""
    if isinstance(error, HttpError):
        summary = f"HttpError (HTTP {error.resp.status}): {error.reason}"
    elif error is not None:
        summary = f"{type(error).__name__}: {error}"
    else:
        summary = next((line for line in reversed(error_text.splitlines()) if line.strip()), "Unknown error")
    # Keep the error visible even when a calendar client collapses long text.
    excerpt = error_text[-4000:]
    if len(error_text) > 4000:
        excerpt = "[Earlier traceback omitted; see run.log for the full error.]\n" + excerpt
    text = f"Sync error: {summary[:1000]}\n\nFull details: run.log in the Host Helper directory.\n\n{excerpt}"
    # Google Calendar descriptions support HTML. Raw <HttpError ...> would be
    # treated as a tag and hide precisely the status/message we need to see.
    return html.escape(text)


def report_failure(
    error_text: str,
    when: date | None = None,
    *,
    error: Exception | None = None,
) -> bool:
    """Put an all-day 'SYNC FAILED' event on the calendar so a broken run is
    visible on the phone without reading logs on the VPS.

    Obtains its OWN service: the caller has just crashed, possibly before or
    during its own get_service() call, so nothing from the failed run can be
    reused. Never raises -- an alerting failure must not replace the real
    error the caller is about to surface. Returns whether the event landed.

    The event id is derived from the date, so repeated failures on the same day
    update one event instead of stacking up. Deliberately NOT recorded in
    derived_events: these are alerts, not rule output, and must not be swept up
    by the stale-deletion pass. A successful full sync clears these alerts.
    """
    try:
        event_id = google_event_id(f"sync-failure-{(when or date.today()).isoformat()}")
        day = (when or date.today()).isoformat()
        body = {
            "id": event_id,
            "summary": FAILURE_EVENT_TITLE,
            "description": _failure_description(error_text, error),
            # Explicitly restore visibility if the same day's alert was cleared
            # by an earlier successful run and a later run fails again.
            "status": "confirmed",
            "start": {"date": day},
            "end": {"date": (date.fromisoformat(day) + timedelta(days=1)).isoformat()},
            "reminders": {"useDefault": False},
            "colorId": FAILURE_COLOR_ID,
        }
        upsert_calendar_event(get_service(), event_id, body)
        return True
    except Exception as alert_error:  # noqa: BLE001 -- alerting must never raise
        print(f"Could not write failure event to calendar: {alert_error!r}")
        return False


def clear_failure_events(when: date | None = None) -> int:
    """Clear this app's failure alerts through today after a successful sync.

    Includes alerts from older versions, which were not tracked in SQLite.
    Verify the deterministic date-based ID before deleting any search result.
    Never turn a successful sync into a failure if alert cleanup is unavailable;
    log the problem so the remaining alerts can be retried next run.
    """
    cleared = 0
    try:
        today = when or date.today()
        service = get_service()
        searches = ["SYNC FAILED"]
        if "SYNC FAILED" not in FAILURE_EVENT_TITLE:
            searches.append(FAILURE_EVENT_TITLE)
        event_ids = set()
        for search in searches:
            page_token = None
            while True:
                response = (
                    service.events()
                    .list(
                        calendarId=CALENDAR_ID,
                        q=search,
                        showDeleted=False,
                        maxResults=2500,
                        pageToken=page_token,
                        fields="nextPageToken,items(id,status,start)",
                    )
                    .execute(num_retries=3)
                )
                for event in response.get("items", []):
                    if event.get("status") == "cancelled":
                        continue
                    try:
                        day = date.fromisoformat(event.get("start", {}).get("date", ""))
                    except (ValueError, TypeError):
                        continue
                    expected_id = google_event_id(f"sync-failure-{day.isoformat()}")
                    if day <= today and event.get("id") == expected_id:
                        event_ids.add(expected_id)
                page_token = response.get("nextPageToken")
                if not page_token:
                    break

        # Finish listing before deleting so pagination cannot shift under us.
        for event_id in sorted(event_ids):
            try:
                service.events().delete(calendarId=CALENDAR_ID, eventId=event_id).execute(num_retries=3)
            except HttpError as error:
                if error.resp.status not in (404, 410):
                    raise
            cleared += 1
    except Exception as cleanup_error:  # noqa: BLE001 -- alert cleanup is best-effort
        print(
            f"Could not finish clearing SYNC FAILED events; will retry next successful sync: {cleanup_error!r}"
        )
    return cleared


def sync_calendar() -> None:
    """Full calendar-side sync: push bookings/blocks, delete cancelled events,
    recompute and sync trash tasks. Uses the Calendar service only."""
    service = get_service()

    bookings = fetch_enriched_bookings()
    for booking in bookings:
        if booking.is_blocked:
            print(upsert_blocked_date(service, booking))
        else:
            print(upsert_booking(service, booking))

    removed = cleanup_cancelled_events(service)
    print(f"Cleaned up {removed} cancelled booking event(s)")

    today = date.today()
    tasks = evaluate_trash_rule(bookings, today, today + timedelta(days=60))
    sync_trash_tasks(service, tasks)
    print(f"Synced {len(tasks)} trash task(s)")


if __name__ == "__main__":
    sync_calendar()
