"""Failure alerts must show HTTP diagnostics even in HTML-aware calendars."""

import html
import unittest
from datetime import date
from html.parser import HTMLParser
from unittest.mock import Mock, patch

from googleapiclient.errors import HttpError
from httplib2 import Response

from host_helper import run
from host_helper.calendar_sync import (
    _failure_description,
    clear_failure_events,
    google_event_id,
    report_failure,
)


class VisibleText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.text = ""

    def handle_data(self, data):
        self.text += data


class FailureReportingTests(unittest.TestCase):
    def test_another_failure_after_success_can_restore_same_day_alert(self):
        service = Mock()
        event = {}

        def update(**kwargs):
            def execute():
                event.update(kwargs["body"])
                return dict(event)

            return Mock(execute=execute)

        def delete(**kwargs):
            def execute(num_retries=0):
                event["status"] = "cancelled"
                return {}

            return Mock(execute=execute)

        def list_events(**kwargs):
            return Mock(
                execute=lambda num_retries=0: {
                    "items": [dict(event)] if event.get("status") == "confirmed" else [],
                }
            )

        service.events.return_value.update.side_effect = update
        service.events.return_value.delete.side_effect = delete
        service.events.return_value.list.side_effect = list_events
        with patch("host_helper.calendar_sync.get_service", return_value=service):
            self.assertTrue(report_failure("First failure", date(2026, 10, 6)))
            first_id = event["id"]
            self.assertEqual(clear_failure_events(date(2026, 10, 6)), 1)
            self.assertEqual(event["status"], "cancelled")
            self.assertTrue(report_failure("Second failure", date(2026, 10, 6)))
            self.assertEqual(event["status"], "confirmed")
            self.assertEqual(event["id"], first_id)
            self.assertIn("Second failure", event["description"])
            self.assertEqual(clear_failure_events(date(2026, 10, 6)), 1)

    def test_success_clears_today_and_older_alerts_across_all_pages(self):
        old = {"id": google_event_id("sync-failure-2026-10-05"), "start": {"date": "2026-10-05"}}
        today = {"id": google_event_id("sync-failure-2026-10-06"), "start": {"date": "2026-10-06"}}
        service = Mock()
        service.events.return_value.list.return_value.execute.side_effect = [
            {"items": [old], "nextPageToken": "second"},
            {"items": [], "nextPageToken": "third"},
            {"items": [today, old]},
        ]
        with patch("host_helper.calendar_sync.get_service", return_value=service):
            self.assertEqual(clear_failure_events(date(2026, 10, 6)), 2)
        deleted = {call.kwargs["eventId"] for call in service.events.return_value.delete.call_args_list}
        self.assertEqual(deleted, {old["id"], today["id"]})
        pages = [call.kwargs["pageToken"] for call in service.events.return_value.list.call_args_list]
        self.assertEqual(pages, [None, "second", "third"])

    def test_cleanup_preserves_unrelated_timed_and_future_events(self):
        items = [
            {"id": "personal-event", "start": {"date": "2026-10-06"}},
            {"id": google_event_id("sync-failure-2026-10-07"), "start": {"date": "2026-10-07"}},
            {"id": google_event_id("sync-failure-2026-10-06"), "start": {"dateTime": "2026-10-06T12:00:00Z"}},
            {"id": "malformed-date", "start": {"date": "invalid"}},
            {"id": google_event_id("sync-failure-2026-10-05"), "start": {"date": "2026-10-06"}},
            {
                "id": google_event_id("sync-failure-2026-10-06"),
                "start": {"date": "2026-10-06"},
                "status": "cancelled",
            },
        ]
        service = Mock()
        service.events.return_value.list.return_value.execute.return_value = {"items": items}
        with patch("host_helper.calendar_sync.get_service", return_value=service):
            self.assertEqual(clear_failure_events(date(2026, 10, 6)), 0)
        service.events.return_value.delete.assert_not_called()

    def test_cleanup_failure_is_logged_and_does_not_fail_successful_sync(self):
        event = {"id": google_event_id("sync-failure-2026-10-06"), "start": {"date": "2026-10-06"}}
        service = Mock()
        service.events.return_value.list.return_value.execute.return_value = {"items": [event]}
        service.events.return_value.delete.return_value.execute.side_effect = HttpError(
            Response({"status": "403"}), b'{"error": {"message": "Quota Exceeded"}}'
        )
        with (
            patch("host_helper.calendar_sync.get_service", return_value=service),
            patch("builtins.print") as log,
        ):
            self.assertEqual(clear_failure_events(date(2026, 10, 6)), 0)
        self.assertIn("will retry next successful sync", log.call_args.args[0])
        self.assertIn("Quota Exceeded", log.call_args.args[0])

    def test_already_removed_failure_alert_is_success(self):
        for status in (404, 410):
            with self.subTest(status=status):
                service = Mock()
                service.events.return_value.list.return_value.execute.return_value = {
                    "items": [
                        {"id": google_event_id("sync-failure-2026-10-06"), "start": {"date": "2026-10-06"}},
                    ]
                }
                service.events.return_value.delete.return_value.execute.side_effect = HttpError(
                    Response({"status": str(status)}), b'{"error": {"message": "Gone"}}'
                )
                with patch("host_helper.calendar_sync.get_service", return_value=service):
                    self.assertEqual(clear_failure_events(date(2026, 10, 6)), 1)

    def test_later_page_failure_does_not_partially_delete_search_results(self):
        service = Mock()
        service.events.return_value.list.return_value.execute.side_effect = [
            {
                "items": [
                    {"id": google_event_id("sync-failure-2026-10-06"), "start": {"date": "2026-10-06"}}
                ],
                "nextPageToken": "second",
            },
            RuntimeError("Request failed"),
        ]
        with patch("host_helper.calendar_sync.get_service", return_value=service), patch("builtins.print"):
            self.assertEqual(clear_failure_events(date(2026, 10, 6)), 0)
        service.events.return_value.delete.assert_not_called()

    def test_custom_failure_title_is_searched_for_legacy_alerts(self):
        service = Mock()
        service.events.return_value.list.return_value.execute.side_effect = [
            {"items": []},
            {"items": [{"id": google_event_id("sync-failure-2026-10-06"), "start": {"date": "2026-10-06"}}]},
        ]
        with (
            patch("host_helper.calendar_sync.get_service", return_value=service),
            patch("host_helper.calendar_sync.FAILURE_EVENT_TITLE", "Manager error"),
        ):
            self.assertEqual(clear_failure_events(date(2026, 10, 6)), 1)

    def test_main_clears_alerts_only_after_both_syncs_succeed(self):
        order = []
        with (
            patch("host_helper.run.sync_calendar", side_effect=lambda: order.append("calendar")),
            patch("host_helper.run.sync_tasks", side_effect=lambda: order.append("tasks") or []),
            patch("host_helper.run.clear_failure_events", side_effect=lambda: order.append("clear") or 1),
            patch("builtins.print"),
        ):
            run.main()
        self.assertEqual(order, ["calendar", "tasks", "clear"])

    def test_failed_sync_leaves_failure_alerts_in_place(self):
        for failing_phase in ("sync_calendar", "sync_tasks"):
            with (
                self.subTest(phase=failing_phase),
                patch("host_helper.run.sync_calendar"),
                patch("host_helper.run.sync_tasks", return_value=[]),
                patch("host_helper.run.clear_failure_events") as clear,
                patch(f"host_helper.run.{failing_phase}", side_effect=RuntimeError("Sync failed")),
                patch("builtins.print"),
            ):
                with self.assertRaises(RuntimeError):
                    run.main()
                clear.assert_not_called()

    def test_calendar_html_does_not_hide_http_error(self):
        error = HttpError(
            Response({"status": "429"}),
            b'{"error": {"message": "Too many requests"}}',
            uri="https://example.invalid/tasks",
        )
        traceback = f"Traceback (most recent call last):\n  ...\ngoogleapiclient.errors.HttpError: {error}"
        with (
            patch("host_helper.calendar_sync.get_service"),
            patch("host_helper.calendar_sync.upsert_calendar_event") as upsert,
        ):
            self.assertTrue(report_failure(traceback, date(2026, 10, 6), error=error))
        body = upsert.call_args.args[2]
        description = body["description"]
        parser = VisibleText()
        parser.feed(description)
        self.assertTrue(parser.text.startswith("Sync error: HttpError (HTTP 429): Too many requests"))
        self.assertIn(str(error), parser.text)
        self.assertNotIn("<HttpError", description)
        self.assertEqual(body["start"]["date"], "2026-10-06")
        self.assertEqual(body["end"]["date"], "2026-10-07")

    def test_long_traceback_keeps_summary_and_last_error(self):
        error = ValueError("invalid <value> & input")
        traceback = "Frame information\n" * 1000 + f"ValueError: {error}"
        description = html.unescape(_failure_description(traceback, error))
        self.assertTrue(description.startswith("Sync error: ValueError: invalid <value> & input"))
        self.assertIn("Earlier traceback omitted", description)
        self.assertTrue(description.endswith(f"ValueError: {error}"))
        self.assertLess(len(description), 5500)

    def test_legacy_text_only_caller_still_escapes_error(self):
        description = _failure_description("Traceback\nHttpError: <HttpError 400 returned bad request>")
        parser = VisibleText()
        parser.feed(description)
        self.assertIn("<HttpError 400 returned bad request>", parser.text)

    def test_alert_failure_does_not_replace_original_exception(self):
        with (
            patch("host_helper.calendar_sync.get_service", side_effect=RuntimeError("OAuth unavailable")),
            patch("builtins.print"),
        ):
            self.assertFalse(report_failure("original failure", error=ValueError("original")))


if __name__ == "__main__":
    unittest.main()
