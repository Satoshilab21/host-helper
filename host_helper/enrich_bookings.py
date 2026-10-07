from datetime import date

from . import db
from .block_matcher import match_blocks
from .email_parser import enrich_booking, parse_confirmation_email
from .gmail_fetcher import build_query, get_emails
from .ics_loader import Booking, fetch_bookings

# Columns on the events table that map 1:1 to Booking fields, used to rebuild
# Booking objects from DB rows. Mirrors db._BOOKING_COLUMNS minus uid/summary/
# dates/code, which are handled explicitly below.
_ENRICHMENT_COLUMNS = [
    "guest_name",
    "adults",
    "children",
    "infants",
    "checkin_time",
    "checkout_time",
    "payout_amount",
    "listing_name",
    "listing_id",
    "nightly_rate",
    "nights",
    "cleaning_fee",
    "guest_service_fee",
    "occupancy_taxes",
    "total_guest_paid",
    "room_fee",
    "host_service_fee",
    "guest_location",
    "identity_verified",
    "cancellation_policy",
]


def _row_to_booking(row) -> Booking:
    kwargs = {col: row[col] for col in _ENRICHMENT_COLUMNS}
    if kwargs.get("identity_verified") is not None:
        kwargs["identity_verified"] = bool(kwargs["identity_verified"])
    return Booking(
        uid=row["uid"],
        summary=row["summary"],
        check_in=date.fromisoformat(row["check_in"]),
        check_out=date.fromisoformat(row["check_out"]),
        confirmation_code=row["confirmation_code"],
        # Carried through so the sync layer reuses the id already on the
        # calendar rather than recomputing sha1(uid) -- which would diverge for
        # blocks Airbnb reissued under a new uid, recreating the event.
        google_event_id=row["google_event_id"],
        **kwargs,
    )


def fetch_enriched_bookings() -> list[Booking]:
    bookings = fetch_bookings()

    # Match confirmation emails to bookings by confirmation code, keeping the
    # raw email body per code so we can persist it for later re-parsing.
    # Mail arrives via a direct Gmail forwarding rule, so the sender is Airbnb
    # itself (a manual "Fwd:" used to make the sender the forwarding account).
    query = build_query(from_="automated@airbnb.com", subject="Reservation confirmed")
    emails = get_emails(query=query, max_results=50, include_body=True)

    raw_by_code: dict[str, str] = {}
    parsed_by_code: dict[str, dict] = {}
    for e in emails:
        parsed = parse_confirmation_email(e["subject"], e["body"])
        code = parsed.get("confirmation_code")
        if code:
            parsed_by_code[code] = parsed
            raw_by_code[code] = e["body"]

    reservations = [b for b in bookings if not b.is_blocked]
    live_blocks = [b for b in bookings if b.is_blocked]

    db.init_db()
    with db.get_connection() as conn:
        # --- Reservations: keyed by uid, which Airbnb keeps stable ---
        for booking in reservations:
            code = booking.confirmation_code
            raw = None
            if code and code in parsed_by_code:
                booking = enrich_booking(booking, parsed_by_code[code])
                raw = raw_by_code[code]
            db.upsert_event(conn, booking, "booking", raw_enrichment=raw)

        present_uids = [b.uid for b in reservations]

        # --- Blocks: matched by date overlap, since Airbnb reissues a block
        # under a NEW uid whenever its dates change (it auto-blocks days as they
        # become unbookable, growing a block backward to absorb them). Matching
        # by uid would read as cancel + recreate and churn the calendar daily.
        stored_blocks = db.get_open_blocks(conn)
        rekey_pairs, new_blocks, _gone = match_blocks(live_blocks, stored_blocks, date.today())

        for stored_row, live_block in rekey_pairs:
            # Always call rekey_event, even when the uid is unchanged: it also
            # revives a stale terminal status. A block that an earlier run
            # wrongly cancelled is still in the feed, and upsert_event's CASE
            # guard will not resurrect it on its own.
            db.rekey_event(
                conn,
                stored_row["uid"],
                live_block.uid,
                live_block.check_in.isoformat(),
                live_block.check_out.isoformat(),
            )
            db.upsert_event(conn, live_block, "blocked")
            present_uids.append(live_block.uid)

        # Genuinely new blocks. Airbnb filler (short + starting today/tomorrow)
        # has already been dropped by the matcher and is deliberately NOT
        # persisted -- a stored filler would become a match target tomorrow and
        # corrupt a real block's start date.
        for live_block in new_blocks:
            db.upsert_event(conn, live_block, "blocked")
            present_uids.append(live_block.uid)

        # Anything no longer in the feed transitions by its dates
        # (future check-in -> cancelled, past check-in -> completed).
        db.reconcile_missing_events(conn, present_uids)

        # Return the bookable set (booked + active) to push to the calendar,
        # rebuilt from the DB so it reflects persisted state (google_event_id).
        rows = db.get_bookable_events(conn)

    return [_row_to_booking(r) for r in rows]


if __name__ == "__main__":
    for booking in fetch_enriched_bookings():
        print(booking)
        print()
