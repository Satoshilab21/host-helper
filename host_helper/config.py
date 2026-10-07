import os
from urllib.parse import urlparse

AIRBNB_ICS = os.environ.get("AIRBNB_ICS", "")


def require_airbnb_ics() -> str:
    """Validate the feed only for operations that actually fetch bookings."""
    value = os.environ.get("AIRBNB_ICS", AIRBNB_ICS).strip()
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc or "YOUR_" in value:
        raise ValueError("Set AIRBNB_ICS to your private calendar export URL in .env before syncing.")
    return value


# Default check-in/check-out times, used until email parsing supplies real
# per-booking times. Overridable via .env; TIMEZONE must be an IANA zone name
# (e.g. "America/Denver") since Google Calendar events require an explicit tz.
CHECKIN_TIME = os.environ.get("CHECKIN_TIME", "16:00")
CHECKOUT_TIME = os.environ.get("CHECKOUT_TIME", "10:00")
TIMEZONE = os.environ.get("TIMEZONE", "America/New_York")

# Trash rule (pickup scheduling). Pickup days as Python weekday ints (Mon=0 .. Sun=6).
# MIN_OCCUPIED_NIGHTS: nights of accumulated trash needed before a pickup day is
# triggered by ongoing occupancy alone (a too-recent check-in doesn't count yet).
# A checkout is always its own independent trigger, regardless of night count.
TRASH_PICKUP_WEEKDAYS = [0, 3]  # Monday, Thursday
MIN_OCCUPIED_NIGHTS = 2
TRASH_OUT_TIME = os.environ.get("TRASH_OUT_TIME", "19:00")  # evening before pickup

# Popup reminder lead time for each event type, in minutes before the event's start.
TRASH_REMINDER_MINUTES = int(os.environ.get("TRASH_REMINDER_MINUTES", "5"))

# Google Calendar event colorId (1-11: Lavender, Sage, Grape, Flamingo, Banana,
# Tangerine, Peacock, Graphite, Blueberry, Basil, Tomato).
BOOKING_COLOR_ID = os.environ.get("BOOKING_COLOR_ID", "3")  # Grape
TRASH_COLOR_ID = os.environ.get("TRASH_COLOR_ID", "6")  # Tangerine
BLOCKED_COLOR_ID = os.environ.get("BLOCKED_COLOR_ID", "8")  # Graphite

# Failure alerting: when a run crashes it drops an all-day event on the calendar
# carrying the traceback, so a broken sync is visible on the phone without
# reading logs on the VPS. Tomato (11) to stand out against the normal colours.
FAILURE_COLOR_ID = os.environ.get("FAILURE_COLOR_ID", "11")  # Tomato
FAILURE_EVENT_TITLE = os.environ.get("FAILURE_EVENT_TITLE", "⚠️ Host Helper SYNC FAILED")

# --- Google Tasks rules (maintenance scheduling) ---
# Cleaning Supply Restock: fire after this many guest checkouts. A Family
# (blocked) stay fires early -- this many days before its end -- and resets.
RESTOCK_CHECKOUT_THRESHOLD = int(os.environ.get("RESTOCK_CHECKOUT_THRESHOLD", "5"))
RESTOCK_FAMILY_LEAD_DAYS = int(os.environ.get("RESTOCK_FAMILY_LEAD_DAYS", "2"))

# Clean Out the Grill: fire after this many cumulative booked nights, on the
# checkout day of the stay that crosses the threshold (never mid-stay).
GRILL_NIGHTS_THRESHOLD = int(os.environ.get("GRILL_NIGHTS_THRESHOLD", "30"))

# Check Air Filter: tacks onto any task-day, but only if this many days have
# passed since it last fired.
AIR_FILTER_COOLDOWN_DAYS = int(os.environ.get("AIR_FILTER_COOLDOWN_DAYS", "30"))

# --- Blocked-date ("Family") filler guard ---
# Airbnb auto-blocks days once they can no longer be booked, emitting them as
# short "Airbnb (Not available)" VEVENTs. A brand-new block that is BOTH this
# short AND starts within this many days of today is treated as Airbnb filler
# and skipped entirely -- not stored, not pushed, not fed to the rules.
# Only applies to blocks that match no existing stored block, so a real family
# stay that Airbnb merely extended is never suppressed.
# Set BLOCK_MIN_NIGHTS=0 to disable the guard and render every block.
BLOCK_MIN_NIGHTS = int(os.environ.get("BLOCK_MIN_NIGHTS", "1"))
BLOCK_NEW_LEAD_DAYS = int(os.environ.get("BLOCK_NEW_LEAD_DAYS", "1"))

# --- Heartbeat monitoring ---
# Ping URL for an external heartbeat service (e.g. healthchecks.io). The run
# pings it on success; the service alerts when a ping fails to arrive on
# schedule. This is what catches the failures the calendar alert cannot --
# expired OAuth, cron not firing, the VPS being down, a hung run -- because it
# does not depend on this machine or on Google being reachable.
# Unset means monitoring is simply skipped. Treat the URL as a secret: anyone
# holding it can fake a success ping.
HEALTHCHECK_URL = os.environ.get("HEALTHCHECK_URL", "")
TASK_LIST_TITLE = os.environ.get("TASK_LIST_TITLE", "Host Helper")
