from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from .config import MIN_OCCUPIED_NIGHTS, TRASH_OUT_TIME, TRASH_PICKUP_WEEKDAYS
from .ics_loader import Booking


@dataclass
class TrashTask:
    pickup_date: date  # the pickup day itself (e.g. the Monday or Thursday)
    put_out_at: str  # ISO datetime string, evening before pickup_date


def pickup_days_in_range(start: date, end: date) -> list[date]:
    days = []
    current = start
    while current <= end:
        if current.weekday() in TRASH_PICKUP_WEEKDAYS:
            days.append(current)
        current += timedelta(days=1)
    return days


def occupied_nights(bookings: list[Booking], window_start: date, window_end_exclusive: date) -> int:
    nights: set[date] = set()
    for booking in bookings:
        if booking.summary != "Reserved":
            continue

        night = max(booking.check_in, window_start)
        stay_end = min(booking.check_out, window_end_exclusive)
        while night < stay_end:
            nights.add(night)
            night += timedelta(days=1)

    return len(nights)


def checkout_occurred(bookings: list[Booking], window_start: date, window_end_exclusive: date) -> bool:
    return any(
        booking.summary == "Reserved" and window_start <= booking.check_out < window_end_exclusive
        for booking in bookings
    )


def evaluate_trash_rule(bookings: list[Booking], window_start: date, window_end: date) -> list[TrashTask]:
    last_cleared = window_start
    tasks = []

    for pickup_date in pickup_days_in_range(window_start, window_end):
        nights = occupied_nights(bookings, last_cleared, pickup_date)
        checkout = checkout_occurred(bookings, last_cleared, pickup_date)

        if nights >= MIN_OCCUPIED_NIGHTS or checkout:
            evening_before = pickup_date - timedelta(days=1)
            put_out_at = datetime.combine(evening_before, time.fromisoformat(TRASH_OUT_TIME)).isoformat()
            tasks.append(TrashTask(pickup_date=pickup_date, put_out_at=put_out_at))
            last_cleared = pickup_date

    return tasks


if __name__ == "__main__":
    # Example 1: check-in Sunday 7/19, ongoing stay through 7/27 checkout.
    # Expect: Monday 7/20 skipped (only 1 night), Thursday 7/23 fires (4 nights).
    example_1 = [
        Booking(
            uid="a",
            summary="Reserved",
            check_in=date(2026, 7, 19),
            check_out=date(2026, 7, 27),
            confirmation_code="HM1",
        ),
    ]
    print("Example 1 (Sunday check-in, ongoing stay):")
    for task in evaluate_trash_rule(example_1, date(2026, 7, 19), date(2026, 8, 1)):
        print(" ", task)

    # Example 2: guest checks out Monday 7/20 (after that morning's pickup), nobody
    # else checks in before Thursday. Expect: Monday fires (from prior occupancy),
    # Thursday 7/23 also fires (from the Monday checkout debt), even though empty.
    example_2 = [
        Booking(
            uid="b",
            summary="Reserved",
            check_in=date(2026, 7, 16),
            check_out=date(2026, 7, 20),
            confirmation_code="HM2",
        ),
    ]
    print("Example 2 (checkout Monday, empty until Thursday):")
    for task in evaluate_trash_rule(example_2, date(2026, 7, 16), date(2026, 8, 1)):
        print(" ", task)
