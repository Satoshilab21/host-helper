"""Regression tests for block identity matching and filler suppression."""

import unittest
from datetime import date

from host_helper.block_matcher import match_blocks
from host_helper.ics_loader import Booking

TODAY = date(2026, 7, 28)


def block(uid, ci, co):
    return Booking(
        uid=uid,
        summary="Airbnb (Not available)",
        check_in=date.fromisoformat(ci),
        check_out=date.fromisoformat(co),
        confirmation_code=None,
    )


def row(uid, ci, co):
    return {"uid": uid, "check_in": ci, "check_out": co}


class BlockMatcherTests(unittest.TestCase):
    def test_absorption_rekeys_existing_block(self):
        """real absorption 7/29->7/28 is a rekey."""
        rekey, new, gone = match_blocks(
            [block("new-uid", "2026-07-28", "2026-08-09")],
            [row("old-uid", "2026-07-29", "2026-08-09")],
            TODAY,
        )
        self.assertEqual(
            ([(r["uid"], b.uid) for r, b in rekey], [b.uid for b in new], [r["uid"] for r in gone]),
            ([("old-uid", "new-uid")], [], []),
        )

    def test_unchanged_blocks_preserve_matches(self):
        """unchanged blocks match, no churn."""
        rekey, new, gone = match_blocks(
            [block("u1", "2026-10-15", "2026-11-02"), block("u2", "2026-11-15", "2026-12-11")],
            [row("u1", "2026-10-15", "2026-11-02"), row("u2", "2026-11-15", "2026-12-11")],
            TODAY,
        )
        self.assertEqual((len(rekey), len(new), len(gone)), (2, 0, 0))

    def test_near_term_filler_is_suppressed(self):
        """1-night filler starting today is suppressed."""
        rekey, new, gone = match_blocks(
            [block("filler", "2026-07-28", "2026-07-29")],
            [],
            TODAY,
        )
        self.assertEqual((len(rekey), [b.uid for b in new], len(gone)), (0, [], 0))

    def test_future_one_night_block_is_retained(self):
        """1-night block a week out survives the guard."""
        rekey, new, gone = match_blocks(
            [block("real1n", "2026-08-04", "2026-08-05")],
            [],
            TODAY,
        )
        self.assertEqual((len(rekey), [b.uid for b in new], len(gone)), (0, ["real1n"], 0))

    def test_split_assigns_existing_identity_once(self):
        """split: larger overlap wins, other is new, no double-claim."""
        rekey, new, gone = match_blocks(
            [block("big", "2026-09-01", "2026-09-20"), block("small", "2026-09-20", "2026-09-25")],
            [row("stored", "2026-09-01", "2026-09-20")],
            TODAY,
        )
        self.assertEqual(
            ([(r["uid"], b.uid) for r, b in rekey], [b.uid for b in new], [r["uid"] for r in gone]),
            ([("stored", "big")], ["small"], []),
        )

    def test_removed_block_is_reported(self):
        """genuinely removed block is reported gone."""
        rekey, new, gone = match_blocks(
            [],
            [row("vanished", "2026-09-01", "2026-09-10")],
            TODAY,
        )
        self.assertEqual((len(rekey), len(new), [r["uid"] for r in gone]), (0, 0, ["vanished"]))

    def test_adjacent_filler_does_not_steal_family_identity(self):
        """contiguous filler does not steal the family block (overlap wins)."""
        rekey, new, gone = match_blocks(
            [block("adj", "2026-07-28", "2026-07-29"), block("fam", "2026-07-29", "2026-08-09")],
            [row("stored-fam", "2026-07-29", "2026-08-09")],
            TODAY,
        )
        self.assertEqual(
            ([(r["uid"], b.uid) for r, b in rekey], [b.uid for b in new]), ([("stored-fam", "fam")], [])
        )

    def test_second_run_matches_same_block_without_churn(self):
        """idempotent: second run matches itself, nothing new/gone."""
        rekey, new, gone = match_blocks(
            [block("new-uid", "2026-07-28", "2026-08-09")],
            [row("new-uid", "2026-07-28", "2026-08-09")],
            TODAY,
        )
        self.assertEqual(
            ([(r["uid"], b.uid) for r, b in rekey], len(new), len(gone)), ([("new-uid", "new-uid")], 0, 0)
        )
