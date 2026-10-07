from datetime import date

from . import db
from .task_rules import evaluate_task_rules


def main() -> None:
    # Reads whatever booking history is already persisted in the DB (run
    # enrich_bookings / calendar_sync first to populate it). No Google Tasks calls.

    db.init_db()
    with db.get_connection() as conn:
        history = db.get_timeline_events(conn)
        last_fired_str = db.get_rule_state(conn, "air_filter_last_fired")

    last_fired = date.fromisoformat(last_fired_str) if last_fired_str else None

    print(f"Timeline events (booked + active + completed): {len(history)}")
    print(f"Air filter last fired: {last_fired}\n")

    items = evaluate_task_rules(history, last_fired)

    print(f"Tasks that would be created: {len(items)}\n")
    for item in sorted(items, key=lambda i: (i.due_date, i.rule_name)):
        print(f"  {item.due_date}  [{item.rule_name}]  {item.title}  (key={item.key})")


if __name__ == "__main__":
    main()
