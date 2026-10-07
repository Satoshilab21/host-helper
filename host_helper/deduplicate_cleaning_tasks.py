"""Preview or remove duplicate Host Helper cleaning payments for one date.

Run on the machine with working Google Tasks authorization:
    host-helper deduplicate-cleaning --due-date 2026-10-13
    host-helper deduplicate-cleaning --due-date 2026-10-13 --apply

Only tasks sharing the exact Host Helper payment key and due date are grouped.
Different stays and manually created tasks are left alone. Completed copies are
preferred. Deleted task bodies are saved locally before any deletion.
"""

import argparse
import json
from datetime import date, datetime, timezone
from pathlib import Path

from .tasks_sync import delete_task, find_task_list, get_tasks_service, list_tasks, task_key


def duplicate_groups(tasks: list[dict], due_date: date) -> list[list[dict]]:
    groups = {}
    for task in tasks:
        key = task_key(task)
        if not key or not key.startswith("cleaning-payment-"):
            continue
        try:
            # Validate both dates so a mere notes prefix cannot authorize deletion.
            checkin = date.fromisoformat(key[len("cleaning-payment-") : len("cleaning-payment-") + 10])
            checkout = date.fromisoformat(key[len("cleaning-payment-") + 11 :])
        except ValueError:
            continue
        if key != f"cleaning-payment-{checkin.isoformat()}-{checkout.isoformat()}":
            continue
        if checkout != due_date or (task.get("due") or "")[:10] != due_date.isoformat():
            continue
        groups.setdefault(key, []).append(task)
    return [
        sorted(group, key=lambda task: task.get("status") != "completed")
        for group in groups.values()
        if len(group) > 1
    ]


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--due-date", type=date.fromisoformat, required=True)
    parser.add_argument("--apply", action="store_true", help="Delete extras after saving a backup")
    args = parser.parse_args(argv)

    service = get_tasks_service()
    tasklist_id = find_task_list(service)
    if tasklist_id is None:
        raise SystemExit("Host Helper task list not found; no changes made.")
    groups = duplicate_groups(list_tasks(service, tasklist_id), args.due_date)
    for group in groups:
        print(f"{task_key(group[0])}: keep {group[0]['id']} ({group[0].get('status')})")
        for task in group[1:]:
            print(f"  {'DELETE' if args.apply else 'Would delete'} {task['id']}")
    count = sum(len(group) - 1 for group in groups)
    if not args.apply or not count:
        print(f"{count} duplicate(s) {'found' if not args.apply else 'removed'}.")
        return

    backup_dir = Path(".task_backups")
    backup_dir.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup = backup_dir / f"cleaning-{args.due_date}-{stamp}.json"
    backup.write_text(json.dumps({"tasklist_id": tasklist_id, "groups": groups}, indent=2), encoding="utf-8")
    print(f"Backup saved to {backup}")
    for group in groups:
        for task in group[1:]:
            delete_task(service, tasklist_id, task["id"])
    remaining = duplicate_groups(list_tasks(service, tasklist_id), args.due_date)
    if remaining:
        raise SystemExit("Duplicates remain after cleanup; check for an overlapping sync run.")
    print(f"Removed {count} duplicate(s); verified no duplicate payment keys remain for {args.due_date}.")


if __name__ == "__main__":
    main()
