"""Regression coverage for paginated lookup and cleaning payment duplicates."""

import copy
import json
import sqlite3
import unittest
from contextlib import closing
from datetime import date
from unittest.mock import Mock, patch

from googleapiclient.errors import HttpError
from googleapiclient.http import HttpRequest
from httplib2 import Response

from host_helper import db
from host_helper.deduplicate_cleaning_tasks import duplicate_groups
from host_helper.task_rules import TaskItem
from host_helper.tasks_sync import (
    MAX_DUPLICATE_DELETIONS,
    delete_task,
    get_or_create_task_list,
    sync_task_rules,
    upsert_task,
)

KEY = "cleaning-payment-2026-10-10-2026-10-13"
BODY = {"title": "Pay Cleaning Company", "due": "2026-10-13T00:00:00.000Z"}


def payment(task_id, key=KEY, **fields):
    return {"id": task_id, **BODY, "notes": f"open_manager_key:{key}", "status": "needsAction", **fields}


class FakeTasks:
    """Simulate the API's page limit and mutations; record inserted/deleted IDs."""

    def __init__(self, tasks):
        self.items = copy.deepcopy(tasks)
        self.inserted = []
        self.deleted = []
        self.updated = []
        self.pages = []
        self.fail_page = None

    def tasks(self):
        return self

    def list(self, **kwargs):
        def execute(num_retries=0):
            start = int(kwargs.get("pageToken") or 0)
            self.pages.append(start)
            if start == self.fail_page:
                raise RuntimeError("Later page could not be fetched")
            limit = kwargs.get("maxResults", 20)
            end = start + limit
            response = {"items": copy.deepcopy(self.items[start:end])}
            if end < len(self.items):
                response["nextPageToken"] = str(end)
            return response

        return Mock(execute=execute)

    def insert(self, tasklist, body):
        def execute(num_retries=0):
            result = {"id": f"inserted-{len(self.inserted)}", **copy.deepcopy(body)}
            self.items.append(result)
            self.inserted.append(result["id"])
            return result

        return Mock(execute=execute)

    def update(self, tasklist, task, body):
        def execute(num_retries=0):
            existing = next(item for item in self.items if item["id"] == task)
            # Model full replacement, so completion must be explicitly preserved.
            existing.clear()
            existing.update(copy.deepcopy(body))
            self.updated.append(task)
            return copy.deepcopy(existing)

        return Mock(execute=execute)

    def delete(self, tasklist, task):
        def execute(num_retries=0):
            self.items = [item for item in self.items if item["id"] != task]
            self.deleted.append(task)
            return {}

        return Mock(execute=execute)


class TaskSyncTests(unittest.TestCase):
    def test_legacy_task_list_is_renamed_without_creating_another_list(self):
        service = Mock()
        service.tasklists.return_value.list.return_value.execute.return_value = {
            "items": [{"id": "existing", "title": "Open Manager"}],
        }
        self.assertEqual(get_or_create_task_list(service, "Host Helper"), "existing")
        service.tasklists.return_value.update.assert_called_once_with(
            tasklist="existing", body={"title": "Host Helper"}
        )
        service.tasklists.return_value.insert.assert_not_called()

    def test_current_task_list_on_later_page_wins_over_legacy_list(self):
        service = Mock()
        service.tasklists.return_value.list.return_value.execute.side_effect = [
            {"items": [{"id": "legacy", "title": "Open Manager"}], "nextPageToken": "next"},
            {"items": [{"id": "current", "title": "Host Helper"}]},
        ]
        self.assertEqual(get_or_create_task_list(service, "Host Helper"), "current")
        service.tasklists.return_value.insert.assert_not_called()
        service.tasklists.return_value.update.assert_not_called()

    def test_custom_task_list_does_not_rename_legacy_list(self):
        service = Mock()
        service.tasklists.return_value.list.return_value.execute.return_value = {
            "items": [{"id": "legacy", "title": "Open Manager"}],
        }
        service.tasklists.return_value.insert.return_value.execute.return_value = {"id": "custom"}
        self.assertEqual(get_or_create_task_list(service, "My rental"), "custom")
        service.tasklists.return_value.update.assert_not_called()
        service.tasklists.return_value.insert.assert_called_once_with(body={"title": "My rental"})

    def test_duplicate_delete_failure_keeps_syncing_and_retries_next_run(self):
        service = FakeTasks([payment("original"), payment("duplicate")])
        real_delete = service.delete
        error = HttpError(
            Response({"status": "403"}),
            b'{"error": {"message": "Quota Exceeded", "errors": [{"reason": "quotaExceeded"}]}}',
        )
        service.delete = Mock(return_value=Mock(execute=Mock(side_effect=error)))
        other_key = "pool-2026-10-13"
        items = [
            TaskItem(KEY, "cleaning_payment", BODY["title"], date(2026, 10, 13)),
            TaskItem(other_key, "pool", "Check pool water level", date(2026, 10, 13)),
        ]
        with (
            closing(sqlite3.connect(":memory:")) as conn,
            patch.object(db, "get_connection", return_value=conn),
            patch("host_helper.tasks_sync.evaluate_task_rules", return_value=items),
        ):
            conn.row_factory = sqlite3.Row
            with self.assertLogs("host_helper.tasks_sync", level="WARNING") as logs:
                self.assertEqual(sync_task_rules(service, "list"), items)
            self.assertIn("HTTP 403", logs.output[0])
            self.assertIn("Quota Exceeded", logs.output[0])
            with db.get_connection() as conn:
                rows = db.get_active_derived_events(conn)
                self.assertEqual(len(rows), 2)
                self.assertEqual(next(row for row in rows if row["key"] == KEY)["google_task_id"], "original")
            self.assertEqual(service.pages, [0])  # Entire sync uses one snapshot.
            service.delete = real_delete
            sync_task_rules(service, "list")
        self.assertEqual(service.deleted, ["duplicate"])
        self.assertEqual(len(service.inserted), 1)  # Only the unrelated pool task.
        self.assertEqual(service.pages, [0, 0])

    def test_quota_failure_stops_further_duplicate_deletions_in_run(self):
        other_key = "pool-2026-10-13"
        service = FakeTasks(
            [payment("a"), payment("b"), payment("pool-a", key=other_key), payment("pool-b", key=other_key)]
        )
        error = HttpError(Response({"status": "403"}), b'{"error": {"message": "Quota Exceeded"}}')
        service.delete = Mock(return_value=Mock(execute=Mock(side_effect=error)))
        items = [
            TaskItem(KEY, "cleaning_payment", BODY["title"], date(2026, 10, 13)),
            TaskItem(other_key, "pool", BODY["title"], date(2026, 10, 13)),
        ]
        with (
            closing(sqlite3.connect(":memory:")) as conn,
            patch.object(db, "get_connection", return_value=conn),
            patch("host_helper.tasks_sync.evaluate_task_rules", return_value=items),
        ):
            conn.row_factory = sqlite3.Row
            with self.assertLogs("host_helper.tasks_sync", level="WARNING"):
                sync_task_rules(service, "list")
            self.assertEqual(len(db.get_active_derived_events(conn)), 2)
        self.assertEqual(service.delete.call_count, 1)
        self.assertEqual(service.updated, [])
        self.assertEqual(service.inserted, [])

    def test_cleanup_budget_is_shared_across_tasks_and_resumes_next_run(self):
        other_key = "pool-2026-10-13"
        service = FakeTasks(
            [payment(f"a-{i}") for i in range(15)] + [payment(f"b-{i}", key=other_key) for i in range(15)]
        )
        items = [
            TaskItem(KEY, "cleaning_payment", BODY["title"], date(2026, 10, 13)),
            TaskItem(other_key, "pool", BODY["title"], date(2026, 10, 13)),
        ]
        with (
            closing(sqlite3.connect(":memory:")) as conn,
            patch.object(db, "get_connection", return_value=conn),
            patch("host_helper.tasks_sync.evaluate_task_rules", return_value=items),
        ):
            conn.row_factory = sqlite3.Row
            with self.assertLogs("host_helper.tasks_sync", level="WARNING"):
                sync_task_rules(service, "list")
            self.assertEqual(len(service.deleted), MAX_DUPLICATE_DELETIONS)
            sync_task_rules(service, "list")
        self.assertEqual(len(service.items), 2)
        self.assertEqual(len(service.deleted), 28)
        self.assertEqual(service.updated, [])

    def test_unchanged_task_skips_update_but_changed_due_date_is_updated(self):
        service = FakeTasks([payment("original", due="2026-10-13T00:00:00Z")])
        upsert_task(service, "list", KEY, BODY)
        self.assertEqual(service.updated, [])
        upsert_task(service, "list", KEY, {**BODY, "due": "2026-10-14T00:00:00.000Z"})
        self.assertEqual(service.updated, ["original"])

    def test_delete_uses_real_client_backoff_for_rate_limits_and_server_errors(self):
        for status, reason in [
            (429, "rateLimitExceeded"),
            (503, "backendError"),
            (403, "userRateLimitExceeded"),
        ]:
            with self.subTest(status=status):
                http = Mock()
                content = json.dumps(
                    {"error": {"message": "Temporary error", "errors": [{"reason": reason}]}}
                ).encode()
                http.request.side_effect = [
                    (Response({"status": str(status)}), content),
                    (Response({"status": "204"}), b""),
                ]
                request = HttpRequest(
                    http, lambda response, content: {}, "https://example.invalid/task", method="DELETE"
                )
                request._sleep = Mock()
                request._rand = lambda: 0.1
                service = Mock()
                service.tasks.return_value.delete.return_value = request
                delete_task(service, "list", "duplicate")
                self.assertEqual(http.request.call_count, 2)
                request._sleep.assert_called_once()

    def test_already_deleted_task_is_success_and_permission_error_is_preserved(self):
        for status in (404, 410, 403):
            with self.subTest(status=status):
                http = Mock()
                http.request.return_value = (
                    Response({"status": str(status)}),
                    b'{"error": {"message": "Permission denied"}}',
                )
                service = Mock()
                service.tasks.return_value.delete.return_value = HttpRequest(
                    http, lambda response, content: {}, "https://example.invalid/task", method="DELETE"
                )
                if status == 403:
                    with self.assertRaises(HttpError):
                        delete_task(service, "list", "duplicate")
                else:
                    delete_task(service, "list", "duplicate")
                self.assertEqual(http.request.call_count, 1)

    def test_same_key_twice_in_run_does_not_insert_twice(self):
        service = FakeTasks([])
        index = {}
        first = upsert_task(service, "list", KEY, BODY, task_index=index)
        second = upsert_task(service, "list", KEY, BODY, task_index=index)
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(service.inserted), 1)

    def test_overlapping_pages_do_not_delete_the_task_we_keep(self):
        service = FakeTasks([])
        service.list = Mock(
            return_value=Mock(
                execute=Mock(
                    side_effect=[
                        {"items": [payment("original")], "nextPageToken": "next"},
                        {"items": [payment("original")]},
                    ]
                )
            )
        )
        service.items = [payment("original")]
        self.assertEqual(upsert_task(service, "list", KEY, BODY)["id"], "original")
        self.assertEqual(service.deleted, [])

    def test_existing_payment_beyond_first_page_is_not_inserted_again(self):
        service = FakeTasks(
            [{"id": str(i), "notes": "unrelated"} for i in range(105)] + [payment("original")]
        )
        for _ in range(3):
            self.assertEqual(upsert_task(service, "list", KEY, BODY)["id"], "original")
        self.assertEqual(service.inserted, [])
        self.assertIn(100, service.pages)

    def test_seven_duplicates_across_pages_become_one_completed_task(self):
        service = FakeTasks(
            [payment(f"duplicate-{i}") for i in range(6)]
            + [{"id": str(i)} for i in range(100)]
            + [payment("completed", status="completed", completed="2026-10-13T12:00:00Z")]
        )
        result = upsert_task(service, "list", KEY, BODY)
        self.assertEqual(result["id"], "completed")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["completed"], "2026-10-13T12:00:00Z")
        self.assertEqual(len(service.deleted), 6)
        upsert_task(service, "list", KEY, BODY)
        self.assertEqual(service.inserted, [])
        self.assertEqual(len(service.deleted), 6)

    def test_exact_key_does_not_match_prefix_or_manual_task(self):
        service = FakeTasks([payment("longer", key=KEY + "-other"), payment("manual", notes="")])
        result = upsert_task(service, "list", KEY, BODY)
        self.assertIn(result["id"], service.inserted)
        self.assertEqual(len(service.items), 3)
        self.assertEqual(service.deleted, [])

    def test_later_page_failure_does_not_insert_or_delete(self):
        service = FakeTasks([payment(f"duplicate-{i}") for i in range(101)])
        service.fail_page = 100
        with self.assertRaises(RuntimeError):
            upsert_task(service, "list", KEY, BODY)
        self.assertEqual(service.inserted, [])
        self.assertEqual(service.deleted, [])

    def test_existing_list_on_later_page_is_not_recreated(self):
        service = Mock()
        service.tasklists.return_value.list.return_value.execute.side_effect = [
            {"items": [{"id": "other", "title": "Other"}], "nextPageToken": "next"},
            {"items": [{"id": "ours", "title": "Host Helper"}]},
        ]
        self.assertEqual(get_or_create_task_list(service), "ours")
        service.tasklists.return_value.insert.assert_not_called()
        self.assertEqual(service.tasklists.return_value.list.call_args.kwargs["pageToken"], "next")

    def test_cleanup_only_groups_exact_payment_keys_on_requested_day(self):
        other_key = "cleaning-payment-2026-10-11-2026-10-13"
        tasks = [
            payment("a"),
            payment("b"),
            payment("other-stay", key=other_key),
            payment("manual", notes=""),
            payment("moved", due="2026-10-14T00:00:00Z"),
            payment("malformed-a", key=KEY.replace("10-10-2026", "10-10X2026")),
            payment("malformed-b", key=KEY.replace("10-10-2026", "10-10X2026")),
        ]
        groups = duplicate_groups(tasks, date(2026, 10, 13))
        self.assertEqual([[task["id"] for task in group] for group in groups], [["a", "b"]])
        self.assertEqual(duplicate_groups(tasks, date(2026, 10, 14)), [])


if __name__ == "__main__":
    unittest.main()
