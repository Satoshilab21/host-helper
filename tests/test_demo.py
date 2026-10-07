"""Public demo scenarios exercise real reconciliation without deployment data."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import build_demo


class DemoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with (
            patch("requests.get", side_effect=AssertionError("Demo must not fetch a feed")),
            patch(
                "host_helper.tasks_sync.get_tasks_service", side_effect=AssertionError("No Google account")
            ),
        ):
            cls.data = build_demo.build_data()
        cls.scenarios = {scenario["id"]: scenario for scenario in cls.data["scenarios"]}

    def test_baseline_uses_history_and_real_maintenance_rules(self):
        reminders = self.scenarios["enriched"]["reminders"]
        self.assertEqual(len(reminders), 14)
        payment_dates = [item["date"] for item in reminders if item["title"] == "Pay Cleaning Company"]
        self.assertEqual(payment_dates, ["2026-10-13", "2026-10-26", "2026-10-31"])
        self.assertTrue(any(item["title"] == "Clean out the grill" for item in reminders))

    def test_standard_precedes_enrichment_and_only_shows_reserved_entries(self):
        self.assertEqual(
            [scenario["label"] for scenario in self.data["scenarios"][:2]], ["Standard", "Enrich"]
        )
        standard = self.scenarios["standard"]
        self.assertEqual(len(standard["stays"]), 4)
        self.assertTrue(
            all(stay["title"] == "Reserved" and stay["kind"] == "booking" for stay in standard["stays"])
        )
        self.assertTrue(all(set(stay["details"]) == {"nights"} for stay in standard["stays"]))
        self.assertEqual(standard["reminders"], [])

    def test_enrichment_adds_parsed_guest_details_and_identifies_host_block(self):
        standard = {stay["id"]: stay for stay in self.scenarios["standard"]["stays"]}
        enriched = self.scenarios["enriched"]["stays"]
        self.assertEqual(set(standard), {stay["id"] for stay in enriched})
        for stay in enriched:
            self.assertEqual(
                (stay["start"], stay["end"]), (standard[stay["id"]]["start"], standard[stay["id"]]["end"])
            )
        jamie = next(stay for stay in enriched if stay["title"] == "Jamie Reed")
        self.assertEqual(jamie["details"]["guests"], 3)
        self.assertEqual(jamie["details"]["checkIn"], "4:00 PM")
        self.assertEqual(jamie["details"]["checkOut"], "10:00 AM")
        host_block = next(stay for stay in enriched if stay["kind"] == "family")
        self.assertEqual(host_block["title"], "Host blocked")
        self.assertNotIn("guests", host_block["details"])

    def test_cancellation_removes_booking_and_dependent_reminders(self):
        cancelled = self.scenarios["cancelled"]
        self.assertNotIn("Morgan Ellis", [stay["title"] for stay in cancelled["stays"]])
        self.assertFalse(any(item["title"] == "Clean out the grill" for item in cancelled["reminders"]))
        self.assertFalse(any(item["date"] == "2026-10-26" for item in cancelled["reminders"]))
        self.assertEqual(len(cancelled["changes"]["removed"]), 6)

    def test_extension_preserves_calendar_id_and_moves_maintenance(self):
        original = next(stay for stay in self.scenarios["enriched"]["stays"] if stay["kind"] == "family")
        extended = next(stay for stay in self.scenarios["extended"]["stays"] if stay["kind"] == "family")
        self.assertEqual(original["id"], extended["id"])
        self.assertEqual(extended["end"], "2026-10-20")
        reminders = self.scenarios["extended"]["reminders"]
        self.assertTrue(
            any(item["date"] == "2026-10-18" and "Restock" in item["title"] for item in reminders)
        )
        self.assertFalse(any(item["date"] == "2026-10-16" for item in reminders))

    def test_repeat_sync_preserves_output_and_has_no_duplicates(self):
        for field in ("stays", "reminders", "duplicates"):
            self.assertEqual(self.scenarios["enriched"][field], self.scenarios["repeated"][field])
        self.assertEqual(self.scenarios["repeated"]["changes"], {"added": [], "removed": [], "updated": []})
        self.assertTrue(all(scenario["duplicates"] == 0 for scenario in self.scenarios.values()))

    def test_build_is_reproducible_and_does_not_read_private_files(self):
        with tempfile.TemporaryDirectory() as folder:
            private = Path(folder, ".env")
            private.write_text("AIRBNB_ICS=private-placeholder\n", encoding="utf-8")
            with patch("dotenv.load_dotenv", side_effect=AssertionError("No deployment configuration")):
                data = build_demo.build_site(Path(folder, "public"))
            self.assertEqual(data, self.data)
            self.assertEqual(private.read_text(), "AIRBNB_ICS=private-placeholder\n")
            self.assertNotIn("private-placeholder", json.dumps(data))

    def test_only_allowlisted_public_assets_are_uploaded(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder, "public")
            build_demo.build_site(output)
            self.assertEqual({path.name for path in output.iterdir()}, {*build_demo.ASSETS, "data.js"})
            self.assertNotIn("credentials.json", {path.name for path in output.iterdir()})

    def test_output_with_unexpected_files_is_rejected_without_deleting_them(self):
        with tempfile.TemporaryDirectory() as folder:
            private = Path(folder, "credentials.json")
            private.write_text("private-placeholder", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "unexpected files"):
                build_demo.build_site(Path(folder))
            self.assertEqual(private.read_text(), "private-placeholder")
