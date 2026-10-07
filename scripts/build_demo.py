"""Build a public demo from synthetic stays using the application's sync functions.

No .env, disk database, OAuth files, feed, or Google account is accessed. A local
Google API stand-in and in-memory SQLite let the real reconciliation code run.
Only an explicit list of static assets and sanitized results is published.
"""

import argparse
import copy
import io
import json
import shutil
import sqlite3
from contextlib import ExitStack, redirect_stdout
from datetime import date
from pathlib import Path
from unittest.mock import patch

from googleapiclient.errors import HttpError
from httplib2 import Response

from host_helper import block_matcher, calendar_sync, db, task_rules, tasks_sync, trash_rule
from host_helper.email_parser import enrich_booking, parse_confirmation_email
from host_helper.enrich_bookings import _row_to_booking
from host_helper.ics_loader import Booking

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ("index.html", "styles.css", "app.js", "favicon.svg")
TODAY = date(2026, 10, 11)


class DemoDate(date):
    @classmethod
    def today(cls):
        return TODAY


class LocalRequest:
    def __init__(self, callback):
        self.callback = callback

    def execute(self, **_kwargs):
        return copy.deepcopy(self.callback())


class LocalResource:
    """Small, network-free stand-in for Calendar events and Google Tasks."""

    def __init__(self):
        self.items = {}
        self.created = 0

    def list(self, **_kwargs):
        return LocalRequest(lambda: {"items": list(self.items.values())})

    def insert(self, body, **_kwargs):
        def execute():
            self.created += 1
            identifier = body.get("id", f"sample-task-{self.created}")
            self.items[identifier] = {**copy.deepcopy(body), "id": identifier}
            return self.items[identifier]

        return LocalRequest(execute)

    def update(self, body, **kwargs):
        def execute():
            identifier = kwargs.get("eventId", kwargs.get("task"))
            if identifier not in self.items:
                raise HttpError(Response({"status": "404"}), b'{"error":{"message":"Not found"}}')
            self.items[identifier] = {**copy.deepcopy(body), "id": identifier}
            return self.items[identifier]

        return LocalRequest(execute)

    def delete(self, **kwargs):
        def execute():
            self.items.pop(kwargs.get("eventId", kwargs.get("task")), None)
            return {}

        return LocalRequest(execute)


class LocalGoogle:
    def __init__(self):
        self.calendar = LocalResource()
        self.task_list = LocalResource()

    def events(self):
        return self.calendar

    def tasks(self):
        return self.task_list


def sample_stays(enriched=True):
    stays = [
        Booking("sample-a", "Reserved", date(2026, 10, 9), date(2026, 10, 13), "HMSAMPLEA"),
        Booking("sample-family", "Airbnb (Not available)", date(2026, 10, 14), date(2026, 10, 18), None),
        Booking("sample-b", "Reserved", date(2026, 10, 20), date(2026, 10, 26), "HMSAMPLEB"),
        Booking("sample-c", "Reserved", date(2026, 10, 27), date(2026, 10, 31), "HMSAMPLEC"),
    ]
    if not enriched:
        return stays
    guests = {
        "sample-a": ("Jamie Reed", "2 adults, 1 child"),
        "sample-b": ("Morgan Ellis", "2 adults"),
        "sample-c": ("Alex Parker", "4 adults"),
    }
    result = []
    for stay in stays:
        if stay.is_blocked:
            result.append(stay)
            continue
        name, counts = guests[stay.uid]
        subject = f"Reservation confirmed - {name} arrives Oct {stay.check_in.day}"
        body = (
            f"Check-in      Checkout\n{stay.check_in:%a, %b %d}   {stay.check_out:%a, %b %d}\n"
            f"4:00 PM       10:00 AM\n\nGUESTS\n{counts}\n\nCONFIRMATION CODE\n{stay.confirmation_code}\n"
        )
        result.append(enrich_booking(stay, parse_confirmation_email(subject, body)))
    return result


def standard_snapshot():
    """Show the generic reserved intervals before email enrichment/classification."""
    return {
        "stays": [
            {
                "id": calendar_sync.google_event_id(stay.uid),
                "title": "Reserved",
                "start": stay.check_in.isoformat(),
                "end": stay.check_out.isoformat(),
                "kind": "booking",
                "details": {"nights": stay.duration_nights},
            }
            for stay in sample_stays(enriched=False)
        ],
        "reminders": [],
        "duplicates": 0,
    }


def seed_history(conn):
    for i, (start, end) in enumerate(((1, 4), (7, 11), (13, 17), (22, 27))):
        booking = Booking(f"sample-history-{i}", "Reserved", date(2026, 9, start), date(2026, 9, end), None)
        db.upsert_event(conn, booking, "booking")
        conn.execute("UPDATE events SET status = 'completed' WHERE uid = ?", (booking.uid,))
    conn.commit()


def reconcile(conn, google, stays):
    reservations = [stay for stay in stays if not stay.is_blocked]
    for stay in reservations:
        db.upsert_event(conn, stay, "booking")
    pairs, new, _gone = block_matcher.match_blocks(
        [stay for stay in stays if stay.is_blocked], db.get_open_blocks(conn), TODAY
    )
    present = [stay.uid for stay in reservations]
    for stored, live in pairs:
        db.rekey_event(conn, stored["uid"], live.uid, live.check_in.isoformat(), live.check_out.isoformat())
        db.upsert_event(conn, live, "blocked")
        present.append(live.uid)
    for live in new:
        db.upsert_event(conn, live, "blocked")
        present.append(live.uid)
    db.reconcile_missing_events(conn, present)
    conn.commit()
    bookings = [_row_to_booking(row) for row in db.get_bookable_events(conn)]
    with (
        patch.object(calendar_sync, "fetch_enriched_bookings", return_value=bookings),
        redirect_stdout(io.StringIO()),
    ):
        calendar_sync.sync_calendar()
    tasks_sync.sync_task_rules(google, "sample-list")


def snapshot(google, conn):
    stays, reminders = [], []
    bookings = {row["google_event_id"]: _row_to_booking(row) for row in db.get_bookable_events(conn)}
    for event in google.calendar.items.values():
        start = event["start"].get("date", event["start"].get("dateTime", ""))[:10]
        end = event["end"].get("date", event["end"].get("dateTime", ""))[:10]
        if not (start < "2026-11-01" and end >= "2026-10-01"):
            continue
        if event["summary"] == "Put trash cans out":
            reminders.append(
                {
                    "id": event["id"],
                    "title": "Put trash cans out",
                    "date": start,
                    "kind": "trash",
                    "time": "19:00",
                }
            )
        else:
            booking = bookings[event["id"]]
            details = {"nights": booking.duration_nights}
            if not booking.is_blocked:
                details.update(
                    {
                        "guests": (booking.adults or 0) + (booking.children or 0) + (booking.infants or 0),
                        "checkIn": booking.checkin_time,
                        "checkOut": booking.checkout_time,
                        "confirmation": booking.confirmation_code,
                    }
                )
            stays.append(
                {
                    "id": event["id"],
                    "title": "Host blocked"
                    if event["summary"] == "Family"
                    else event["summary"].split(" (")[0],
                    "start": start,
                    "end": end,
                    "kind": "family" if event["summary"] == "Family" else "booking",
                    "details": details,
                }
            )
    for task in google.task_list.items.values():
        if "2026-10-01" <= task["due"][:10] < "2026-11-01":
            key = tasks_sync.task_key(task)
            reminders.append(
                {"id": key, "title": task["title"], "date": task["due"][:10], "kind": "task", "time": None}
            )
    keys = [tasks_sync.task_key(task) for task in google.task_list.items.values()]
    return {
        "stays": sorted(stays, key=lambda stay: (stay["start"], stay["id"])),
        "reminders": sorted(reminders, key=lambda reminder: (reminder["date"], reminder["title"])),
        "duplicates": len(keys) - len(set(keys)),
    }


def difference(before, after):
    old = {item["id"]: item for item in before["stays"] + before["reminders"]}
    new = {item["id"]: item for item in after["stays"] + after["reminders"]}
    return {
        "added": [new[key] for key in sorted(new.keys() - old.keys())],
        "removed": [old[key] for key in sorted(old.keys() - new.keys())],
        "updated": [new[key] for key in sorted(old.keys() & new.keys()) if new[key] != old[key]],
    }


def build_data():
    definitions = [
        (
            "standard",
            "Standard",
            "The standard calendar: just reserved dates.",
            "A few bookings arrive as generic Reserved entries. Choose Enrich to reveal guest details and identify host-blocked dates.",
            "Standard calendar selected. All entries are labeled Reserved.",
        ),
        (
            "enriched",
            "Enrich",
            "From reserved dates to useful booking details.",
            "Sample confirmation emails add guest names, guest counts, and arrival times. The host's blocked dates become Host blocked. Select a booking to explore the details.",
            "Bookings enriched with invented guest names and details. Host-blocked dates identified.",
        ),
        (
            "cancelled",
            "Cancel a booking",
            "One cancellation. Everything follows.",
            "Morgan Ellis's booking is cancelled. Its calendar event and payment reminder disappear. Fewer guest nights also remove the grill check and its pool reminder at the end of the month.",
            "Morgan Ellis's booking and its dependent reminders were removed.",
        ),
        (
            "extended",
            "Extend family stay",
            "More family time. The same calendar event.",
            "The family stay now ends October 20 instead of October 18. Its calendar identity is preserved, and the restock and pool reminders move from October 16 to October 18.",
            "Family dates updated; upkeep moved two days later.",
        ),
        (
            "repeated",
            "Sync again",
            "Same bookings. Same reminders.",
            "The same input is synchronized a second time. Existing events and task reminders are reused, so the schedule stays the same and no duplicates are added.",
            "Second sync complete. No duplicate events or tasks.",
        ),
    ]
    scenarios = []
    # Fix both the reference date and configurable rules. Deployment .env files
    # are never loaded, and ambient settings cannot change public sample results.
    with ExitStack() as stack:
        for module in (db, task_rules, calendar_sync):
            stack.enter_context(patch.object(module, "date", DemoDate))
        for module, values in (
            (
                task_rules,
                {
                    "RESTOCK_CHECKOUT_THRESHOLD": 5,
                    "RESTOCK_FAMILY_LEAD_DAYS": 2,
                    "GRILL_NIGHTS_THRESHOLD": 30,
                    "AIR_FILTER_COOLDOWN_DAYS": 30,
                },
            ),
            (block_matcher, {"BLOCK_MIN_NIGHTS": 1, "BLOCK_NEW_LEAD_DAYS": 1}),
            (
                trash_rule,
                {"TRASH_PICKUP_WEEKDAYS": [0, 3], "MIN_OCCUPIED_NIGHTS": 2, "TRASH_OUT_TIME": "19:00"},
            ),
            (
                calendar_sync,
                {"CHECKIN_TIME": "16:00", "CHECKOUT_TIME": "10:00", "TIMEZONE": "America/New_York"},
            ),
        ):
            for name, value in values.items():
                stack.enter_context(patch.object(module, name, value))
        for identifier, label, title, description, announcement in definitions:
            if identifier == "standard":
                scenarios.append(
                    {
                        "id": identifier,
                        "label": label,
                        "title": title,
                        "description": description,
                        "announcement": announcement,
                        **standard_snapshot(),
                        "changes": {"added": [], "removed": [], "updated": []},
                    }
                )
                continue
            conn = sqlite3.connect(":memory:")
            conn.row_factory = sqlite3.Row
            google = LocalGoogle()
            try:
                conn.executescript(db.SCHEMA)
                seed_history(conn)
                with (
                    patch.object(db, "get_connection", return_value=conn),
                    patch.object(calendar_sync, "get_service", return_value=google),
                ):
                    stays = sample_stays()
                    reconcile(conn, google, stays)
                    before = standard_snapshot() if identifier == "enriched" else snapshot(google, conn)
                    if identifier == "cancelled":
                        stays = [stay for stay in stays if stay.uid != "sample-b"]
                    elif identifier == "extended":
                        family = next(stay for stay in stays if stay.is_blocked)
                        family.uid = "sample-family-extended"
                        family.check_out = date(2026, 10, 20)
                    if identifier != "enriched":
                        reconcile(conn, google, stays)
                    after = snapshot(google, conn)
                scenarios.append(
                    {
                        "id": identifier,
                        "label": label,
                        "title": title,
                        "description": description,
                        "announcement": announcement,
                        **after,
                        "changes": difference(before, after),
                    }
                )
            finally:
                conn.close()
    return {"month": "2026-10", "today": TODAY.isoformat(), "scenarios": scenarios}


def build_site(output):
    output = Path(output).resolve()
    if output in (ROOT, ROOT / "demo"):
        raise ValueError("Choose a separate output directory, such as _site.")
    if output.exists() and any(
        path.name not in {*ASSETS, "data.js"} or not path.is_file() or path.is_symlink()
        for path in output.iterdir()
    ):
        raise ValueError("Demo output contains unexpected files. Choose a clean output directory.")
    data = build_data()
    output.mkdir(parents=True, exist_ok=True)
    for name in ASSETS:
        shutil.copyfile(ROOT / "demo" / name, output / name)
    (output / "data.js").write_text(
        "window.HOST_HELPER_DEMO = " + json.dumps(data, ensure_ascii=True, indent=2) + ";\n", encoding="utf-8"
    )
    return data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "_site")
    args = parser.parse_args()
    data = build_site(args.output)
    print(f"Built {len(data['scenarios'])} synthetic scenarios in {args.output}")


if __name__ == "__main__":
    main()
