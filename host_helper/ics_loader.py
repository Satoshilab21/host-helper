import re
from dataclasses import dataclass
from datetime import date

import requests
from icalendar import Calendar

from .config import require_airbnb_ics

CONFIRMATION_CODE_RE = re.compile(r"HM[A-Z0-9]+")


@dataclass
class Booking:
    uid: str
    summary: str | None
    check_in: date
    check_out: date
    confirmation_code: str | None

    # Email-sourced enrichment (email enrichment). All optional and None until an
    # Airbnb confirmation email is matched to this booking by confirmation_code
    # and merged in -- see enrich_booking() in email_parser.py.
    guest_name: str | None = None
    adults: int | None = None
    children: int | None = None
    infants: int | None = None
    checkin_time: str | None = None  # e.g. "4:00 PM", as Airbnb wrote it
    checkout_time: str | None = None
    payout_amount: str | None = None  # e.g. "$1,000.00" -- kept as the raw string for now
    listing_name: str | None = None
    listing_id: str | None = None

    # Financial breakdown (raw strings, e.g. "$200.00" -- not parsed to Decimal yet)
    nightly_rate: str | None = None
    nights: int | None = None
    cleaning_fee: str | None = None
    guest_service_fee: str | None = None
    occupancy_taxes: str | None = None
    total_guest_paid: str | None = None
    room_fee: str | None = None
    host_service_fee: str | None = None

    guest_location: str | None = None  # e.g. "Springfield, IL"
    identity_verified: bool | None = None
    cancellation_policy: str | None = None  # e.g. "Firm"

    # The Google Calendar event id already assigned to this booking, read back
    # from the DB. Normally sha1(uid), but for blocks that Airbnb reissued
    # under a new uid it is the ORIGINAL id, deliberately preserved so the
    # calendar event is updated in place instead of deleted and recreated.
    # None for bookings not yet pushed; callers fall back to sha1(uid).
    google_event_id: str | None = None

    @property
    def is_blocked(self) -> bool:
        # A blocked/unavailable date range (owner block, Airbnb padding, etc.),
        # not a real guest reservation. Never has a confirmation code.
        return self.summary == "Airbnb (Not available)"

    @property
    def duration_nights(self) -> int:
        return (self.check_out - self.check_in).days


def extract_confirmation_code(description: str | None) -> str | None:
    if not description:
        return None

    match = CONFIRMATION_CODE_RE.search(description)
    return match.group(0) if match else None


def parse_bookings(cal: Calendar) -> list[Booking]:
    booking_list = []
    for event in cal.walk("VEVENT"):
        booking_list.append(
            Booking(
                uid=str(event.get("UID")),
                summary=str(event.get("SUMMARY")),
                check_in=event.get("DTSTART").dt,
                check_out=event.get("DTEND").dt,
                confirmation_code=extract_confirmation_code(event.get("DESCRIPTION")),
            )
        )
    return booking_list


def fetch_airbnb_calendar(ics_link: str) -> Calendar:
    return Calendar.from_ical(requests.get(ics_link).text)


def fetch_bookings() -> list[Booking]:
    cal = fetch_airbnb_calendar(require_airbnb_ics())
    return parse_bookings(cal)


if __name__ == "__main__":
    for booking in fetch_bookings():
        print(booking)
