"""Command line entry points; load deployment settings only when running commands."""

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

from . import __version__


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="host-helper",
        description="Synchronize vacation rental bookings and maintenance reminders.",
    )
    parser.add_argument("--version", action="version", version=f"Host Helper {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("sync", help="Run the complete calendar and task sync")
    auth = commands.add_parser("authorize", help="Authorize Google APIs through an SSH tunnel")
    auth.add_argument("tokens", nargs="*", help="Optional token filenames to reauthorize")
    preview = commands.add_parser("preview", help="Preview scheduling without Google event/task writes")
    preview.add_argument("target", choices=["calendar", "tasks", "trash"])
    cleanup = commands.add_parser("deduplicate-cleaning", help="Inspect duplicate cleaning payments")
    cleanup.add_argument("--due-date", required=True)
    cleanup.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)

    # Imports and test discovery never read .env or require account credentials.
    # Import service modules after loading settings, so their defaults reflect .env.
    load_dotenv(Path.cwd() / ".env")
    if args.command == "sync" or (args.command == "preview" and args.target != "tasks"):
        from .config import require_airbnb_ics

        try:
            require_airbnb_ics()
        except ValueError as error:
            parser.error(str(error))

    if args.command == "sync":
        from .run import execute

        execute()
    elif args.command == "authorize":
        from .authorize import main as authorize

        authorize(args.tokens)
    elif args.command == "preview":
        if args.target == "calendar":
            from .dry_run_calendar_sync import main as preview_run
        elif args.target == "tasks":
            from .dry_run_tasks import main as preview_run
        else:
            from .dry_run_trash import main as preview_run
        preview_run()
    else:
        from .deduplicate_cleaning_tasks import main as deduplicate

        cleanup_args = ["--due-date", args.due_date]
        if args.apply:
            cleanup_args.append("--apply")
        deduplicate(cleanup_args)


if __name__ == "__main__":
    main(sys.argv[1:])
