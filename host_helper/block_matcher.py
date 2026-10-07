"""Identity matching for Airbnb blocked-date ranges.

Airbnb derives a block's UID from its date range, so ANY change to the dates
produces an entirely new UID -- and blocks carry no DESCRIPTION, so the UID is
the only identifier the feed gives us. Treating that UID as a stable identity
makes a shifted block look like "old block cancelled, new block created", which
churns the Google Calendar (delete + recreate) every time Airbnb nudges a
boundary.

Airbnb nudges boundaries daily: once a date can no longer be booked, it is
marked unavailable. If it is adjacent to an existing block, the block grows
backward to absorb it. If it sits isolated in a gap, Airbnb emits a standalone
short "filler" block.

This module reconstructs the identity Airbnb destroys, by matching live blocks
to stored blocks on date OVERLAP instead of UID.

Pure: no DB, no network, no implicit date.today() -- `today` is passed in so the
whole thing is unit-testable.
"""

from datetime import date, timedelta

from .config import BLOCK_MIN_NIGHTS, BLOCK_NEW_LEAD_DAYS


def _overlap_days(a_in: date, a_out: date, b_in: date, b_out: date) -> int:
    """Days of overlap between two half-open [check_in, check_out) intervals."""
    lo = max(a_in, b_in)
    hi = min(a_out, b_out)
    return max(0, (hi - lo).days)


def _touches(a_in: date, a_out: date, b_in: date, b_out: date) -> bool:
    """True if the intervals overlap OR are contiguous (one ends where the
    other begins). Contiguity is what lets an absorbed filler day be recognised
    as a continuation of the block it was merged into."""
    return a_in <= b_out and b_in <= a_out


def is_filler_block(check_in: date, check_out: date, today: date) -> bool:
    """A brand-new block that is both short AND starts at/near today is Airbnb
    auto-blocking an unbookable day, not a family stay.

    Only ever applied to blocks that matched NOTHING stored -- a real family
    block that Airbnb merely extended is matched, so it never reaches here.
    A real family block is created in advance, so when first seen it starts in
    the future; filler only exists for days that are already unbookable.

    BLOCK_MIN_NIGHTS=0 disables the guard entirely.
    """
    if BLOCK_MIN_NIGHTS <= 0:
        return False
    nights = (check_out - check_in).days
    return nights <= BLOCK_MIN_NIGHTS and check_in <= today + timedelta(days=BLOCK_NEW_LEAD_DAYS)


def match_blocks(live_blocks, stored_rows, today: date):
    """Match live .ics blocks to stored block rows by date overlap.

    Args:
        live_blocks: ics_loader.Booking objects where is_blocked is True.
        stored_rows: sqlite3.Row (or any mapping) with 'uid', 'check_in',
            'check_out' -- ISO date strings, as stored.
        today: reference date for the filler guard.

    Returns (rekey_pairs, new_blocks, gone_rows):
        rekey_pairs: [(stored_row, live_block)] -- same block, dates may have
            moved. Caller should re-key the stored row to the new uid, keeping
            its google_event_id so the calendar event survives.
        new_blocks: live blocks matching nothing stored, with filler already
            excluded. Caller should insert these normally.
        gone_rows: stored rows matching no live block -- genuinely removed.

    Matching is greedy by descending overlap; each stored row and each live
    block is claimed at most once. That is deterministic and handles the
    split/merge cases without ever double-claiming a stored row (which would
    otherwise cause a spurious cancellation).
    """
    candidates = []
    for row in stored_rows:
        s_in = date.fromisoformat(row["check_in"])
        s_out = date.fromisoformat(row["check_out"])
        for block in live_blocks:
            if _touches(s_in, s_out, block.check_in, block.check_out):
                overlap = _overlap_days(s_in, s_out, block.check_in, block.check_out)
                candidates.append((overlap, row, block))

    # Sort by overlap descending; tie-break on dates so the result is stable
    # rather than dependent on input ordering.
    candidates.sort(key=lambda c: (-c[0], c[1]["check_in"], c[2].check_in))

    claimed_rows = set()
    claimed_blocks = set()
    rekey_pairs = []
    for _overlap, row, block in candidates:
        if row["uid"] in claimed_rows or block.uid in claimed_blocks:
            continue
        claimed_rows.add(row["uid"])
        claimed_blocks.add(block.uid)
        rekey_pairs.append((row, block))

    new_blocks = [
        b
        for b in live_blocks
        if b.uid not in claimed_blocks and not is_filler_block(b.check_in, b.check_out, today)
    ]
    gone_rows = [r for r in stored_rows if r["uid"] not in claimed_rows]

    return rekey_pairs, new_blocks, gone_rows
