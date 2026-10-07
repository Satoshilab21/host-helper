import json
from datetime import date, timedelta

from .calendar_sync import build_blocked_event_body, build_event_body, build_trash_event_body
from .enrich_bookings import fetch_enriched_bookings
from .trash_rule import evaluate_trash_rule


def main() -> None:
    bookings = fetch_enriched_bookings()

    print(f"Bookings: {len(bookings)}\n")
    for booking in bookings:
        body = build_blocked_event_body(booking) if booking.is_blocked else build_event_body(booking)
        print(json.dumps(body, indent=2))
        print()

    today = date.today()
    tasks = evaluate_trash_rule(bookings, today, today + timedelta(days=60))

    print(f"Trash tasks: {len(tasks)}\n")
    for task in tasks:
        body = build_trash_event_body(task)
        print(json.dumps(body, indent=2))
        print()


if __name__ == "__main__":
    main()
