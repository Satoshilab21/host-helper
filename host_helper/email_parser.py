"""Parsers for Airbnb "Reservation confirmed" emails.

Targets Airbnb's own text/plain MIME part, which is what arrives when Gmail
forwards the message directly (a forwarding rule, not a manual "Fwd:"). That
format differs substantially from Gmail's text rendering of the HTML part,
which earlier hand-forwarded mail produced:

  - Labels are UPPERCASE section headers ("CONFIRMATION CODE", "GUEST PAID").
  - Most label/value pairs sit on ONE line, separated by runs of spaces
    ("Cleaning fee   $180.00") rather than value-on-the-next-line.
  - Check-in and checkout are laid out as two COLUMNS:
        Check-in      Checkout
        Mon, Aug 10   Sun, Aug 16
        4:00 PM       10:00 AM
  - Times use U+202F NARROW NO-BREAK SPACE before AM/PM, not a plain space.

Every extractor returns None rather than raising, so one changed label degrades
a single field instead of failing the whole parse.
"""

import re
from dataclasses import replace

from .ics_loader import CONFIRMATION_CODE_RE, Booking

# Airbnb separates a label from its value with a run of 2+ spaces, and uses
# U+202F (narrow no-break space) inside times.
_COLUMN_GAP = re.compile(r"\s{2,}")
_NARROW_NBSP = " "


def _normalise(text: str) -> str:
    return text.replace(_NARROW_NBSP, " ").replace("\xa0", " ")


def _lines(body: str) -> list[str]:
    return _normalise(body).splitlines()


def _find_line_index(lines: list[str], label: str) -> int | None:
    """Index of the first line whose text starts with `label` (case-insensitive).
    Matches both a standalone header and a 'Label   value' pair."""
    target = label.casefold()
    for i, line in enumerate(lines):
        if line.strip().casefold().startswith(target):
            return i
    return None


def _value_on_label_line(lines: list[str], label: str) -> str | None:
    """Value sharing a line with its label, e.g. 'Cleaning fee   $180.00'."""
    index = _find_line_index(lines, label)
    if index is None:
        return None
    parts = _COLUMN_GAP.split(lines[index].strip())
    return parts[-1].strip() if len(parts) >= 2 else None


def _value_after_label(lines: list[str], label: str) -> str | None:
    """Value on the next non-blank line, for standalone headers like
    'CONFIRMATION CODE'."""
    index = _find_line_index(lines, label)
    if index is None:
        return None
    for line in lines[index + 1 :]:
        if line.strip():
            return line.strip()
    return None


def extract_confirmation_code_from_email(body: str) -> str | None:
    if not body:
        return None
    match = CONFIRMATION_CODE_RE.search(body)
    return match.group(0) if match else None


def extract_guest_name(body: str, subject: str) -> str | None:
    # Subject is "Reservation confirmed - <Name> arrives <Month Day>". Tolerates
    # a leading "Fwd: " so older hand-forwarded mail still yields a name.
    if not subject:
        return None
    match = re.search(r"Reservation confirmed - (.+?) arrives", subject)
    return match.group(1).strip() if match else None


def extract_guest_counts(body: str) -> tuple[int | None, int | None, int | None]:
    """From the line after the GUESTS header, e.g. '3 adults, 1 child'.
    Categories absent from the text are 0; a missing section gives all None."""
    if not body:
        return None, None, None

    guests_line = _value_after_label(_lines(body), "GUESTS")
    if guests_line is None:
        return None, None, None

    def count(pattern: str) -> int:
        match = re.search(pattern, guests_line, re.IGNORECASE)
        return int(match.group(1)) if match else 0

    return count(r"(\d+)\s+adult"), count(r"(\d+)\s+child"), count(r"(\d+)\s+infant")


def extract_checkin_checkout_times(body: str) -> tuple[str | None, str | None]:
    """Check-in and checkout are side-by-side columns:

        Check-in      Checkout
        Mon, Aug 10   Sun, Aug 16
        4:00 PM       10:00 AM

    Take the two times from the second non-blank line after the header row.
    """
    if not body:
        return None, None

    lines = _lines(body)
    index = _find_line_index(lines, "Check-in")
    if index is None:
        return None, None

    following = [line.strip() for line in lines[index + 1 :] if line.strip()]
    if len(following) < 2:
        return None, None

    times = _COLUMN_GAP.split(following[1])  # [0] is the date row
    if len(times) < 2:
        return None, None

    return times[0].strip(), times[1].strip()


def extract_payout(body: str) -> str | None:
    """The host's actual take-home, from 'YOU EARN   $1,183.00'. Distinct from
    GUEST PAID / TOTAL, which are guest-facing figures."""
    if not body:
        return None
    return _value_on_label_line(_lines(body), "YOU EARN")


ROOM_TYPE_MARKERS = {"entire home/apt", "private room", "shared room"}


def extract_listing_name(body: str) -> str | None:
    """Nearest non-blank, non-URL line above the room-type marker."""
    if not body:
        return None

    lines = _lines(body)
    index = next(
        (i for i, line in enumerate(lines) if line.strip().casefold() in ROOM_TYPE_MARKERS),
        None,
    )
    if index is None:
        return None

    for line in reversed(lines[:index]):
        stripped = line.strip()
        if stripped and not stripped.startswith(("http", "[http")):
            return stripped
    return None


LISTING_ID_RE = re.compile(r"airbnb\.com/rooms/(\d+)")


def extract_listing_id(body: str) -> str | None:
    if not body:
        return None
    match = LISTING_ID_RE.search(body)
    return match.group(1) if match else None


def extract_guest_location(body: str) -> str | None:
    """Sits just below the 'Identity verified' line in the guest block."""
    if not body:
        return None
    return _value_after_label(_lines(body), "Identity verified")


def extract_identity_verified(body: str) -> bool | None:
    if not body:
        return None
    return "identity verified" in body.casefold()


def extract_cancellation_policy(body: str) -> str | None:
    if not body:
        return None
    match = re.search(r"cancellation policy for guests is (\w+)", body, re.IGNORECASE)
    return match.group(1) if match else None


def extract_financial_breakdown(body: str) -> dict:
    """Fee lines are 'Label   $amount' pairs. 'Cleaning fee' appears twice --
    once under GUEST PAID, once under HOST PAYOUT -- so the host-side figure is
    read from the HOST PAYOUT section only."""
    empty = {
        "nightly_rate": None,
        "nights": None,
        "cleaning_fee": None,
        "guest_service_fee": None,
        "occupancy_taxes": None,
        "total_guest_paid": None,
        "room_fee": None,
        "host_service_fee": None,
    }
    if not body:
        return empty

    lines = _lines(body)

    # "$200.00 x 6 nights   $1,200.00" -- the first line under GUEST PAID.
    nightly_rate = nights = None
    rate_line = _value_after_label(lines, "GUEST PAID")
    if rate_line:
        match = re.search(r"(\$[\d,.]+)\s*x\s*(\d+)\s*nights?", rate_line)
        if match:
            nightly_rate = match.group(1)
            nights = int(match.group(2))

    # Scope the host-side cleaning fee to lines after the HOST PAYOUT header.
    host_index = _find_line_index(lines, "HOST PAYOUT")
    host_lines = lines[host_index:] if host_index is not None else lines

    # Night count varies ("6 nights room fee"), and the host service fee carries
    # a percentage ("Host service fee (15.5%)"), so match on a pattern.
    def value_matching(candidates: list[str], pattern: str) -> str | None:
        for line in candidates:
            if re.match(pattern, line.strip(), re.IGNORECASE):
                parts = _COLUMN_GAP.split(line.strip())
                if len(parts) >= 2:
                    return parts[-1].strip()
        return None

    return {
        "nightly_rate": nightly_rate,
        "nights": nights,
        "cleaning_fee": _value_on_label_line(host_lines, "Cleaning fee"),
        "guest_service_fee": _value_on_label_line(lines, "Guest service fee"),
        "occupancy_taxes": _value_on_label_line(lines, "Occupancy taxes"),
        "total_guest_paid": _value_on_label_line(lines, "TOTAL (USD)"),
        "room_fee": value_matching(host_lines, r"\d+\s+nights?\s+room fee"),
        "host_service_fee": value_matching(host_lines, r"Host service fee"),
    }


def _safe(fn, *args):
    try:
        return fn(*args)
    except Exception:
        return None


def parse_confirmation_email(subject: str, body: str) -> dict:
    adults, children, infants = _safe(extract_guest_counts, body) or (None, None, None)
    checkin_time, checkout_time = _safe(extract_checkin_checkout_times, body) or (None, None)
    financials = _safe(extract_financial_breakdown, body) or {}

    return {
        "confirmation_code": _safe(extract_confirmation_code_from_email, body),
        "guest_name": _safe(extract_guest_name, body, subject),
        "adults": adults,
        "children": children,
        "infants": infants,
        "checkin_time": checkin_time,
        "checkout_time": checkout_time,
        "payout_amount": _safe(extract_payout, body),
        "listing_name": _safe(extract_listing_name, body),
        "listing_id": _safe(extract_listing_id, body),
        "guest_location": _safe(extract_guest_location, body),
        "identity_verified": _safe(extract_identity_verified, body),
        "cancellation_policy": _safe(extract_cancellation_policy, body),
        **financials,
    }


def enrich_booking(booking: Booking, parsed_email: dict) -> Booking:
    updates = {
        field: value
        for field, value in parsed_email.items()
        if value is not None and getattr(booking, field, None) is None
    }
    return replace(booking, **updates)
