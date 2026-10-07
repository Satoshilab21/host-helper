"""Regression tests for persistent block identity and collision recovery."""

import sqlite3
import unittest
from datetime import date
from unittest.mock import patch

from host_helper import db


class RekeyEventTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(db.SCHEMA)
        self.addCleanup(self.conn.close)
        fixed_date = patch("host_helper.db.date")
        clock = fixed_date.start()
        clock.today.return_value = date(2026, 10, 7)
        clock.fromisoformat.side_effect = date.fromisoformat
        self.addCleanup(fixed_date.stop)

    def insert(self, uid, ci, co, gid, status="booked"):
        self.conn.execute(
            "INSERT INTO events (uid, type, status, summary, check_in, check_out, "
            "google_event_id, updated_at) VALUES (?, 'blocked', ?, "
            "'Airbnb (Not available)', ?, ?, ?, 'now')",
            (uid, status, ci, co, gid),
        )

    def rows(self):
        return self.conn.execute("SELECT * FROM events ORDER BY uid").fetchall()

    def test_rekey_preserves_calendar_identity(self):
        self.insert("old", "2026-07-29", "2026-08-09", "GID_A")
        db.rekey_event(self.conn, "old", "new", "2026-07-28", "2026-08-09")
        self.assertEqual(
            [(r["uid"], r["check_in"], r["google_event_id"]) for r in self.rows()],
            [("new", "2026-07-28", "GID_A")],
        )

    def seed_collision(self, old_gid="GID_ORIGINAL"):
        self.insert("old", "2026-07-28", "2026-08-09", old_gid)
        self.insert("new", "2026-07-29", "2026-08-09", "GID_SURVIVOR")
        db.rekey_event(self.conn, "old", "new", "2026-07-29", "2026-08-09")

    def test_collision_does_not_raise_integrity_error(self):
        self.seed_collision()

    def test_collision_keeps_original_calendar_event(self):
        self.seed_collision()
        self.assertEqual(
            [(r["uid"], r["check_in"], r["check_out"], r["google_event_id"]) for r in self.rows()],
            [("new", "2026-07-29", "2026-08-09", "GID_ORIGINAL")],
        )

    def test_missing_old_calendar_id_uses_surviving_id(self):
        self.seed_collision(None)
        self.assertEqual(self.rows()[0]["google_event_id"], "GID_SURVIVOR")

    def test_collision_leaves_one_row(self):
        self.seed_collision(None)
        self.assertEqual(len(self.rows()), 1)

    def self_rekey(self):
        self.insert("same", "2026-07-28", "2026-08-09", "GID_X")
        db.rekey_event(self.conn, "same", "same", "2026-07-27", "2026-08-09")

    def test_self_rekey_updates_dates_and_keeps_id(self):
        self.self_rekey()
        self.assertEqual(
            [(r["uid"], r["check_in"], r["google_event_id"]) for r in self.rows()],
            [("same", "2026-07-27", "GID_X")],
        )

    def test_self_rekey_keeps_one_row(self):
        self.self_rekey()
        self.assertEqual(len(self.rows()), 1)

    def revive_collision(self):
        self.insert("old-active", "2026-07-28", "2026-08-09", "GID_LIVE", "active")
        self.insert("reissued", "2026-07-29", "2026-08-09", None, "cancelled")
        db.rekey_event(self.conn, "old-active", "reissued", "2026-07-29", "2026-08-09")

    def test_cancelled_collision_is_revived(self):
        self.revive_collision()
        self.assertEqual(
            [(r["uid"], r["status"], r["check_in"], r["google_event_id"]) for r in self.rows()],
            [("reissued", "active", "2026-07-29", "GID_LIVE")],
        )

    def test_revived_collision_keeps_one_row(self):
        self.revive_collision()
        self.assertEqual(len(self.rows()), 1)

    def test_cancelled_future_block_is_booked(self):
        self.insert("same", "2026-10-15", "2026-11-02", "GID_Y", "cancelled")
        db.rekey_event(self.conn, "same", "same", "2026-10-15", "2026-11-02")
        self.assertEqual((self.rows()[0]["status"], self.rows()[0]["google_event_id"]), ("booked", "GID_Y"))

    def test_started_block_is_active(self):
        self.insert("past", "2020-01-01", "2020-01-10", "G1", "cancelled")
        db.rekey_event(self.conn, "past", "past", "2020-01-01", "2020-01-10")
        self.assertEqual(self.rows()[0]["status"], "active")
