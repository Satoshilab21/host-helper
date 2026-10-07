"""Daily orchestrator -- the single entry point for the cron job.

Runs the full pipeline in order:
  1. Calendar sync: pull the .ics, enrich from email, reconcile booking states,
     push bookings/blocks, delete cancelled events, sync trash tasks.
  2. Task sync: run the Google Tasks rules (restock, grill, cleaning payment, pool, air filter)
     against the now-updated booking history.

Task sync runs after calendar sync so the events table (and its booked ->
active -> completed state transitions) is up to date before the counter-based
rules read history from it.

Two independent alerting paths, because each covers the other's blind spot:

  - On failure, an all-day "SYNC FAILED" event carrying the traceback is
    written to the calendar, so a broken run is visible on the phone rather
    than only in run.log. Useless when the failure IS Google auth.
    After both calendar and task sync succeed, previous failure alerts are
    cleared automatically.
  - A heartbeat ping to an external service on success, so its absence raises
    the alarm. Catches expired auth, cron not firing, the VPS being down, and
    hung runs -- none of which can write a calendar event.

The exception is always re-raised afterwards, preserving the non-zero exit
status and the full traceback in the log.
"""

import traceback
import urllib.request

from .calendar_sync import clear_failure_events, report_failure, sync_calendar
from .config import HEALTHCHECK_URL
from .tasks_sync import sync_tasks


def ping(suffix: str = "") -> None:
    """Signal run status to the heartbeat service.

    suffix: "/start" when beginning, "/fail" on error, "" on success.
    Never raises -- monitoring must not break or mask the run it watches.
    Silently does nothing when HEALTHCHECK_URL is unset.
    """
    if not HEALTHCHECK_URL:
        return
    try:
        urllib.request.urlopen(HEALTHCHECK_URL + suffix, timeout=10)
    except Exception as error:  # noqa: BLE001 -- monitoring is best-effort
        print(f"Heartbeat ping{suffix or ' (success)'} failed: {error!r}")


def main() -> None:
    print("=== Calendar sync ===")
    sync_calendar()

    print("\n=== Task sync ===")
    synced = sync_tasks()
    for item in synced:
        print(f"  {item.due_date}  [{item.rule_name}]  {item.title}")
    print(f"Synced {len(synced)} task(s)")
    cleared = clear_failure_events()
    print(f"Cleared {cleared} previous SYNC FAILED event(s)")


def execute() -> None:
    """Run the sync with failure alerts, heartbeat signals, and a failing exit status."""
    # "/start" lets the service measure duration and spot a run that hangs
    # rather than one that never began.
    ping("/start")
    try:
        main()
    except Exception as error:
        # Alerting is additive: report, then re-raise so the exit code and the
        # full traceback still reach cron and run.log unchanged.
        details = traceback.format_exc()
        print(details)
        if report_failure(details, error=error):
            print("Wrote SYNC FAILED event to the calendar.")
        # Explicit failure ping: alerts now instead of waiting for the missed
        # heartbeat window, and still works when Google is what broke.
        ping("/fail")
        raise
    else:
        ping()


if __name__ == "__main__":
    from .cli import main as cli_main

    cli_main(["sync"])
