import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

# Keep the original state filename during the branding/package migration.
# State belongs to the working directory, never the installed Python package.
DB_PATH = Path.cwd() / "open_manager.db"

# The events columns that map 1:1 to a Booking dataclass field. Kept in sync
# with ics_loader.Booking -- if you add an enrichment field there, add the
# matching column to the events schema and it flows through automatically.
_ICS_COLUMNS = ["uid", "summary", "check_in", "check_out", "confirmation_code"]

# Email-sourced. These are "sticky": once populated they are never overwritten
# with NULL, because the .ics pull carries none of them and an email that fails
# to match on a given run (Gmail hiccup, template drift, an old message that no
# longer matches the search) would otherwise WIPE previously-enriched data.
# A later email with a real value still updates them -- only NULL is ignored.
_EMAIL_COLUMNS = [
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

_BOOKING_COLUMNS = _ICS_COLUMNS + _EMAIL_COLUMNS

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    uid                 TEXT PRIMARY KEY,
    type                TEXT NOT NULL,          -- 'booking' | 'blocked'
    status              TEXT NOT NULL DEFAULT 'active',  -- 'active' | 'cancelled'
    summary             TEXT,
    check_in            TEXT NOT NULL,          -- ISO date 'YYYY-MM-DD'
    check_out           TEXT NOT NULL,
    confirmation_code   TEXT,
    google_event_id     TEXT,

    -- Email enrichment (nullable; NULL for blocks and unmatched bookings)
    guest_name          TEXT,
    adults              INTEGER,
    children            INTEGER,
    infants             INTEGER,
    checkin_time        TEXT,
    checkout_time       TEXT,
    payout_amount       TEXT,
    listing_name        TEXT,
    listing_id          TEXT,
    nightly_rate        TEXT,
    nights              INTEGER,
    cleaning_fee        TEXT,
    guest_service_fee   TEXT,
    occupancy_taxes     TEXT,
    total_guest_paid    TEXT,
    room_fee            TEXT,
    host_service_fee    TEXT,
    guest_location      TEXT,
    identity_verified   INTEGER,                -- 0/1/NULL
    cancellation_policy TEXT,
    raw_enrichment      TEXT,                   -- original email body, for re-parsing

    updated_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS derived_events (
    key                 TEXT PRIMARY KEY,       -- stable synthetic key e.g. 'trash-2026-07-23'
    rule_name           TEXT NOT NULL,          -- 'trash' | 'cleaning_restock' | 'grill' | 'pool' | 'air_filter'
    sink                TEXT NOT NULL,          -- 'calendar' | 'tasks'
    due_date            TEXT NOT NULL,
    google_event_id     TEXT,                   -- set when sink='calendar'
    google_task_id      TEXT,                   -- set when sink='tasks'
    status              TEXT NOT NULL DEFAULT 'active',  -- 'active' | 'stale'
    updated_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rule_state (
    rule_name           TEXT PRIMARY KEY,       -- e.g. 'cleaning_restock_counter'
    value               TEXT,                   -- stringified counter / date / JSON
    updated_at          TEXT NOT NULL
);
"""


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row  # dict-like row access by column name
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    with get_connection() as conn:
        conn.executescript(SCHEMA)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# --- events ---


def _status_for_present(check_in: date) -> str:
    # A booking present in the .ics: 'booked' until its check-in day arrives,
    # then 'active'. Recomputed every run so booked->active happens on its own.
    return "active" if check_in <= date.today() else "booked"


def upsert_event(
    conn: sqlite3.Connection, booking, event_type: str, raw_enrichment: str | None = None
) -> None:
    """Insert or update an events row for a booking PRESENT in the current .ics
    pull. Status is derived from check-in vs today ('booked' -> 'active'), and
    recomputed on every upsert. Terminal states ('completed'/'cancelled') are
    never resurrected: if a row somehow reappears in the feed after completing/
    cancelling, its status is left alone. google_event_id is preserved."""
    values = {col: getattr(booking, col) for col in _BOOKING_COLUMNS}
    values["check_in"] = booking.check_in.isoformat()
    values["check_out"] = booking.check_out.isoformat()
    if values.get("identity_verified") is not None:
        values["identity_verified"] = int(values["identity_verified"])

    values["type"] = event_type
    values["raw_enrichment"] = raw_enrichment
    values["status"] = _status_for_present(booking.check_in)
    values["updated_at"] = _now()

    columns = list(values.keys())
    placeholders = ", ".join(f":{c}" for c in columns)

    # On conflict:
    #  - .ics columns overwrite freely (the feed is authoritative for them)
    #  - email columns use COALESCE so a NULL never erases stored enrichment
    #  - google_event_id is untouched (owned by the sync layer)
    #  - status only advances from non-terminal states
    assignments = []
    for column in columns:
        if column in ("uid", "status"):
            continue
        if column in _EMAIL_COLUMNS or column == "raw_enrichment":
            assignments.append(f"{column} = COALESCE(excluded.{column}, events.{column})")
        else:
            assignments.append(f"{column} = excluded.{column}")
    updates = ", ".join(assignments)
    updates += (
        ", status = CASE WHEN events.status IN ('completed', 'cancelled') "
        "THEN events.status ELSE excluded.status END"
    )

    conn.execute(
        f"INSERT INTO events ({', '.join(columns)}) VALUES ({placeholders}) "
        f"ON CONFLICT(uid) DO UPDATE SET {updates}",
        values,
    )


def set_event_google_id(conn: sqlite3.Connection, uid: str, google_event_id: str) -> None:
    conn.execute(
        "UPDATE events SET google_event_id = ?, updated_at = ? WHERE uid = ?",
        (google_event_id, _now(), uid),
    )


def get_cancelled_events_needing_cleanup(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Cancelled bookings that still have a Google Calendar event to delete.

    Completed bookings are intentionally NOT here -- their calendar events are
    left in place as historical records.

    Blocked dates are deliberately EXCLUDED too: Airbnb reissues blocks under
    new uids, so a block reaching 'cancelled' usually means identity matching
    missed, not that the user deleted it. Leaving a stale grey event the user
    can remove by hand is far better than silently deleting a real family stay.
    """
    return conn.execute(
        "SELECT * FROM events WHERE status = 'cancelled' AND google_event_id IS NOT NULL "
        "AND type != 'blocked'"
    ).fetchall()


def clear_event_google_id(conn: sqlite3.Connection, uid: str) -> None:
    conn.execute(
        "UPDATE events SET google_event_id = NULL, updated_at = ? WHERE uid = ?",
        (_now(), uid),
    )


def get_active_events(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM events WHERE status = 'active'").fetchall()


def get_open_blocks(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Blocked-date rows eligible for identity matching against the current
    .ics pull -- see block_matcher.match_blocks. Airbnb changes a block's UID
    whenever its dates change, so blocks can't be matched by uid the way
    reservations are.

    'cancelled' rows are INCLUDED: a block cancelled by an earlier run (before
    matching existed, or because a shift briefly made it look missing) is often
    still in the feed and must be recoverable. rekey_event revives the status
    when it matches. Only 'completed' rows are excluded -- those are genuinely
    in the past and matching them would resurrect old blocks."""
    return conn.execute(
        "SELECT * FROM events WHERE type = 'blocked' "
        "AND status IN ('booked', 'active', 'cancelled') ORDER BY check_in"
    ).fetchall()


def rekey_event(conn: sqlite3.Connection, old_uid: str, new_uid: str, check_in: str, check_out: str) -> None:
    """Move an events row to a new uid and date range, PRESERVING its
    google_event_id and status.

    Used when Airbnb reissues a block under a new UID because its dates shifted.
    Keeping google_event_id is the whole point: the Google Calendar event keeps
    its original id (a sha1 of the ORIGINAL uid) and is updated in place, rather
    than being deleted and recreated. After this, the stored google_event_id is
    deliberately no longer sha1(uid) -- callers must use the stored value, not
    recompute it.

    Collision handling: a row may ALREADY exist under new_uid -- e.g. a DB
    written before block matching existed, where the shifted block was inserted
    as a separate row alongside the original. A bare UPDATE would then fail the
    uid PRIMARY KEY constraint and abort the whole sync. Instead we merge:
    carry the older row's google_event_id onto the surviving row (so the
    long-lived calendar event is the one kept) and delete the duplicate.
    """
    # Reaching here means the block IS in the current feed, so any terminal
    # status on the row we land on is stale and must be cleared -- otherwise the
    # block stays 'cancelled' forever and never reaches the calendar, because
    # upsert_event's CASE guard deliberately refuses to resurrect terminal rows.
    revived = _status_for_present(date.fromisoformat(check_in))

    if old_uid == new_uid:
        # Same uid, dates may have changed. Plain update -- must NOT fall into
        # the merge branch below, which would delete the row it just updated.
        conn.execute(
            "UPDATE events SET check_in = ?, check_out = ?, status = ?, updated_at = ? WHERE uid = ?",
            (check_in, check_out, revived, _now(), new_uid),
        )
        return

    existing = conn.execute("SELECT google_event_id FROM events WHERE uid = ?", (new_uid,)).fetchone()

    if existing is not None:
        old = conn.execute("SELECT google_event_id FROM events WHERE uid = ?", (old_uid,)).fetchone()
        surviving_gid = (old["google_event_id"] if old else None) or existing["google_event_id"]
        conn.execute(
            "UPDATE events SET check_in = ?, check_out = ?, google_event_id = ?, "
            "status = ?, updated_at = ? WHERE uid = ?",
            (check_in, check_out, surviving_gid, revived, _now(), new_uid),
        )
        conn.execute("DELETE FROM events WHERE uid = ?", (old_uid,))
        return

    conn.execute(
        "UPDATE events SET uid = ?, check_in = ?, check_out = ?, status = ?, updated_at = ? WHERE uid = ?",
        (new_uid, check_in, check_out, revived, _now(), old_uid),
    )


def get_bookable_events(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Events that should appear on the calendar: 'booked' (future) and 'active'
    (current). Excludes 'completed' (past -- stays as a historical calendar
    event, but not re-pushed here) and 'cancelled' (removed)."""
    return conn.execute(
        "SELECT * FROM events WHERE status IN ('booked', 'active') ORDER BY check_in"
    ).fetchall()


def get_timeline_events(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """The full known booking timeline for the counter/task rules: booked
    (future), active (current), and completed (past) -- everything except
    cancelled. Ordered by check_out so the rules can walk it chronologically.
    Includes future stays so counters look ahead (e.g. grill nights accrue for
    upcoming stays, Family-stay restock tasks schedule before the stay ends)."""
    return conn.execute(
        "SELECT * FROM events WHERE status IN ('booked', 'active', 'completed') ORDER BY check_out"
    ).fetchall()


def reconcile_missing_events(conn: sqlite3.Connection, present_uids: list[str]) -> list[str]:
    """Transition bookings that are NO LONGER in the .ics pull, by their DATES:
      - check-in still in the future -> 'cancelled'  (a real cancellation; the
        stay never started, so its calendar event should be deleted)
      - check-in already passed      -> 'completed'  (ran its course or ended
        early; still counts as occupancy history for the rules, and its
        calendar event is left in place as a record)
    Already-terminal rows ('completed'/'cancelled') are left untouched.
    Returns the uids newly marked 'cancelled' so the caller can delete their
    calendar events. With no present_uids, changes nothing (fail-safe: an
    empty/failed .ics fetch must not rewrite the whole table)."""
    if not present_uids:
        return []

    rows = conn.execute(
        "SELECT uid, status, check_in FROM events WHERE status IN ('booked', 'active')"
    ).fetchall()
    present = set(present_uids)
    missing = [r for r in rows if r["uid"] not in present]

    today = date.today()
    cancelled = []
    for row in missing:
        # Decide from the DATES, not the stored status. The stored status can be
        # stale: if no run happened between a booking's check-in and its removal
        # from the feed (a missed cron day, or a short stay that began and ended
        # between runs), it is still recorded as 'booked' despite having run --
        # and trusting that would mark a completed stay 'cancelled', deleting a
        # real event from the calendar and under-counting the task rules.
        # Identical to the old behaviour whenever runs are healthy.
        new_status = "cancelled" if date.fromisoformat(row["check_in"]) > today else "completed"
        conn.execute(
            "UPDATE events SET status = ?, updated_at = ? WHERE uid = ?",
            (new_status, _now(), row["uid"]),
        )
        if new_status == "cancelled":
            cancelled.append(row["uid"])
    return cancelled


# --- derived_events ---


def upsert_derived_event(
    conn: sqlite3.Connection,
    key: str,
    rule_name: str,
    sink: str,
    due_date: str,
    google_event_id: str | None = None,
    google_task_id: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO derived_events
            (key, rule_name, sink, due_date, google_event_id, google_task_id, status, updated_at)
        VALUES (:key, :rule_name, :sink, :due_date, :google_event_id, :google_task_id, 'active', :updated_at)
        ON CONFLICT(key) DO UPDATE SET
            rule_name = excluded.rule_name,
            sink = excluded.sink,
            due_date = excluded.due_date,
            google_event_id = COALESCE(excluded.google_event_id, derived_events.google_event_id),
            google_task_id = COALESCE(excluded.google_task_id, derived_events.google_task_id),
            status = 'active',
            updated_at = excluded.updated_at
        """,
        {
            "key": key,
            "rule_name": rule_name,
            "sink": sink,
            "due_date": due_date,
            "google_event_id": google_event_id,
            "google_task_id": google_task_id,
            "updated_at": _now(),
        },
    )


def get_active_derived_events(conn: sqlite3.Connection, rule_name: str | None = None) -> list[sqlite3.Row]:
    if rule_name is None:
        return conn.execute("SELECT * FROM derived_events WHERE status = 'active'").fetchall()
    return conn.execute(
        "SELECT * FROM derived_events WHERE status = 'active' AND rule_name = ?", (rule_name,)
    ).fetchall()


def mark_missing_derived_stale(
    conn: sqlite3.Connection, rule_name: str, present_keys: list[str]
) -> list[sqlite3.Row]:
    """For one rule, mark active derived_events whose key is NOT in present_keys
    as 'stale'. Returns the full rows marked stale, so the caller can delete
    them from Google (calendar/tasks) -- this is what closes the trash rule's
    stale-event gap."""
    rows = conn.execute(
        "SELECT * FROM derived_events WHERE status = 'active' AND rule_name = ?", (rule_name,)
    ).fetchall()
    present = set(present_keys)
    stale = [r for r in rows if r["key"] not in present]

    for row in stale:
        conn.execute(
            "UPDATE derived_events SET status = 'stale', updated_at = ? WHERE key = ?",
            (_now(), row["key"]),
        )
    return stale


def delete_derived_event(conn: sqlite3.Connection, key: str) -> None:
    conn.execute("DELETE FROM derived_events WHERE key = ?", (key,))


# --- rule_state (generic key/value counters) ---


def get_rule_state(conn: sqlite3.Connection, rule_name: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM rule_state WHERE rule_name = ?", (rule_name,)).fetchone()
    return row["value"] if row else default


def set_rule_state(conn: sqlite3.Connection, rule_name: str, value: str) -> None:
    conn.execute(
        """
        INSERT INTO rule_state (rule_name, value, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(rule_name) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """,
        (rule_name, value, _now()),
    )


if __name__ == "__main__":
    init_db()
    print(f"Initialized database at {DB_PATH}")
