"""Clean-clone startup and command compatibility without real account access."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from host_helper import cli, config, db, tasks_sync

ROOT = Path(__file__).resolve().parents[1]


class CommandLineTests(unittest.TestCase):
    def run_clean(self, args, dotenv=None):
        env = dict(os.environ)
        env.pop("AIRBNB_ICS", None)
        env.pop("TASK_LIST_TITLE", None)
        env["PYTHONPATH"] = str(ROOT)
        with tempfile.TemporaryDirectory() as folder:
            if dotenv is not None:
                Path(folder, ".env").write_text(dotenv, encoding="utf-8")
            return subprocess.run(
                [sys.executable, *args], cwd=folder, env=env, capture_output=True, text=True, timeout=30
            )

    def test_help_needs_no_deployment_configuration(self):
        result = self.run_clean(["-m", "host_helper", "--help"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("sync", result.stdout)
        self.assertIn("authorize", result.stdout)

    def test_sync_without_configuration_has_actionable_error(self):
        result = self.run_clean(["-m", "host_helper", "sync"])
        self.assertEqual(result.returncode, 2)
        self.assertIn("Set AIRBNB_ICS", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_sync_loads_working_directory_configuration_before_services(self):
        code = (
            "import sys,types; "
            "from host_helper.cli import main; "
            "sys.modules['host_helper.run'] = types.SimpleNamespace(execute=lambda: "
            "print(__import__('host_helper.config',fromlist=['TASK_LIST_TITLE']).TASK_LIST_TITLE)); "
            "main(['sync'])"
        )
        result = self.run_clean(
            ["-c", code],
            dotenv="AIRBNB_ICS=https://example.invalid/bookings\nTASK_LIST_TITLE=Synthetic task list\n",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "Synthetic task list")

    def test_import_all_modules_without_configuration_or_io(self):
        code = (
            "import importlib,pkgutil,host_helper; "
            "[importlib.import_module(m.name) for m in pkgutil.iter_modules("
            "host_helper.__path__, host_helper.__name__ + '.') if not m.name.endswith('.__main__')]"
        )
        result = self.run_clean(["-c", code])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")

    def test_root_cron_entry_point_remains_available(self):
        result = self.run_clean([str(ROOT / "run.py"), "--help"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("host-helper sync", result.stdout)

    def test_root_authorization_entry_point_remains_available(self):
        result = self.run_clean([str(ROOT / "authorize.py"), "--help"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("tokens", result.stdout)

    def test_preview_tasks_does_not_require_a_feed(self):
        with (
            patch("host_helper.cli.load_dotenv"),
            patch("host_helper.dry_run_tasks.main") as preview,
            patch("host_helper.config.require_airbnb_ics", side_effect=AssertionError("No feed needed")),
        ):
            cli.main(["preview", "tasks"])
        preview.assert_called_once_with()

    def test_placeholder_url_is_rejected_without_disclosing_it(self):
        with patch.dict(
            os.environ,
            {"AIRBNB_ICS": "https://www.airbnb.com/calendar/ical/YOUR_LISTING_ID.ics?t=YOUR_ICS_TOKEN"},
        ):
            with self.assertRaisesRegex(ValueError, "Set AIRBNB_ICS"):
                config.require_airbnb_ics()

    def test_private_identifiers_keep_their_original_storage_contract(self):
        self.assertEqual(db.DB_PATH.name, "open_manager.db")
        self.assertEqual(
            tasks_sync.task_key({"notes": "open_manager_key:cleaning-payment-2026-10-10-2026-10-13"}),
            "cleaning-payment-2026-10-10-2026-10-13",
        )
