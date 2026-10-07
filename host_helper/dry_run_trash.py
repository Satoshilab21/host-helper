from datetime import date, timedelta

from .ics_loader import fetch_bookings
from .trash_rule import evaluate_trash_rule


def main() -> None:
    bookings = fetch_bookings()

    today = date.today()
    window_end = today + timedelta(days=60)

    print(f"Evaluating trash rule for {today} .. {window_end}")
    print(f"Bookings loaded: {len(bookings)}")
    print()

    tasks = evaluate_trash_rule(bookings, today, window_end)

    if not tasks:
        print("No trash tasks generated in this window.")
    else:
        for task in tasks:
            print(f"pickup {task.pickup_date}  ->  put cans out at {task.put_out_at}")


if __name__ == "__main__":
    main()
