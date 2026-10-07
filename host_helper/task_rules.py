"""Google Tasks rules (maintenance scheduling).

Five rules, each producing TaskItems routed to Google Tasks:

  1. Cleaning Supply Restock -- counter of guest checkouts; fires every
     RESTOCK_CHECKOUT_THRESHOLD checkouts. A Family (blocked) stay instead
     fires RESTOCK_FAMILY_LEAD_DAYS before its end and resets the counter to 0.
     Family stays do NOT otherwise count toward the checkout counter.

  2. Clean Out the Grill -- counter of cumulative booked nights; fires on the
     checkout day of the stay that pushes the total past GRILL_NIGHTS_THRESHOLD.
     Never scheduled mid-stay (checkout day == guest departed, per trash rule).

  3. Pay Cleaning Company -- fires on EVERY guest (non-Family) checkout, no
     threshold. Deliberately excluded from the tack-on day-set (see below) --
     an online payment task shouldn't cause Pool/Air Filter to fire on every
     single checkout.

  4. Check Pool Water Level -- tacks onto ANY day that already has a restock
     or grill task, at most once per day. No cooldown. Does NOT tack onto
     "Pay Cleaning Company" days.

  5. Check Air Filter -- tacks on like Pool (same day-set, same exclusion of
     Pay Cleaning Company), but only if AIR_FILTER_COOLDOWN_DAYS have passed
     since it last fired.

Counters are recomputed from persisted booking history each run (db.get_history_
events), so re-running is idempotent -- rule_state stores only high-water marks
(what has already fired), never a mutable tally that could double-count.
"""

from dataclasses import dataclass
from datetime import date, timedelta

from .config import (
    AIR_FILTER_COOLDOWN_DAYS,
    GRILL_NIGHTS_THRESHOLD,
    RESTOCK_CHECKOUT_THRESHOLD,
    RESTOCK_FAMILY_LEAD_DAYS,
)

BLOCKED_SUMMARY = "Airbnb (Not available)"


@dataclass
class TaskItem:
    key: str  # stable synthetic key, e.g. "restock-2026-07-24"
    rule_name: str  # 'cleaning_restock' | 'grill' | 'pool' | 'air_filter'
    title: str
    due_date: date


@dataclass
class _Stay:
    """A minimal view of an events-table row the rules care about."""

    check_in: date
    check_out: date
    is_blocked: bool


def _rows_to_stays(rows) -> list[_Stay]:
    stays = []
    for r in rows:
        stays.append(
            _Stay(
                check_in=date.fromisoformat(r["check_in"]),
                check_out=date.fromisoformat(r["check_out"]),
                is_blocked=(r["summary"] == BLOCKED_SUMMARY),
            )
        )
    stays.sort(key=lambda s: s.check_out)
    return stays


# --- Rule 1: Cleaning Supply Restock ---


def evaluate_restock(stays: list[_Stay]) -> list[TaskItem]:
    """Walk stays chronologically. Each guest checkout increments a running
    count; at the threshold, emit a restock task on that checkout day and reset.
    A Family (blocked) stay instead emits a task RESTOCK_FAMILY_LEAD_DAYS before
    its end and resets the count -- Family stays don't otherwise count."""
    tasks = []
    count = 0

    for stay in stays:
        if stay.is_blocked:
            # Guard: a block shorter than the lead time can't have a meaningful
            # "N days before it ends" task -- the due date would land before the
            # block even starts. Such short blocks are almost always Airbnb
            # auto-blocking an unbookable day rather than a family stay, and
            # firing here would also wrongly reset the checkout counter.
            # (Belt-and-braces: block_matcher already drops filler upstream.)
            if (stay.check_out - stay.check_in).days < RESTOCK_FAMILY_LEAD_DAYS:
                continue

            due = stay.check_out - timedelta(days=RESTOCK_FAMILY_LEAD_DAYS)
            tasks.append(
                TaskItem(
                    key=f"restock-{due.isoformat()}",
                    rule_name="cleaning_restock",
                    title="Restock cleaning supplies (before family stay ends)",
                    due_date=due,
                )
            )
            count = 0
        else:
            count += 1
            if count >= RESTOCK_CHECKOUT_THRESHOLD:
                due = stay.check_out
                tasks.append(
                    TaskItem(
                        key=f"restock-{due.isoformat()}",
                        rule_name="cleaning_restock",
                        title="Restock cleaning supplies",
                        due_date=due,
                    )
                )
                count = 0

    return tasks


# --- Rule 2: Clean Out the Grill ---


def evaluate_grill(stays: list[_Stay]) -> list[TaskItem]:
    """Accumulate booked nights across guest stays (blocked/Family stays don't
    count -- no guest grilling). When the running total crosses the threshold,
    emit a grill task on that stay's checkout day and subtract the threshold
    from the running total (carry the remainder forward)."""
    tasks = []
    nights_total = 0

    for stay in stays:
        if stay.is_blocked:
            continue
        nights_total += (stay.check_out - stay.check_in).days
        if nights_total >= GRILL_NIGHTS_THRESHOLD:
            due = stay.check_out
            tasks.append(
                TaskItem(
                    key=f"grill-{due.isoformat()}",
                    rule_name="grill",
                    title="Clean out the grill",
                    due_date=due,
                )
            )
            nights_total -= GRILL_NIGHTS_THRESHOLD

    return tasks


# --- Rule 3: Pay Cleaning Company ---


def evaluate_cleaning_payment(stays: list[_Stay]) -> list[TaskItem]:
    """One task per guest (non-Family) checkout, no threshold, no counter.
    Intentionally kept separate from base_tasks in the coordinator so it never
    becomes a tack-on trigger for Pool/Air Filter."""
    tasks = []
    for stay in stays:
        if stay.is_blocked:
            continue
        due = stay.check_out
        # Key includes check_in, not just check_out, so two distinct stays
        # that happen to check out the same day don't collide and silently
        # overwrite one another's payment task.
        tasks.append(
            TaskItem(
                key=f"cleaning-payment-{stay.check_in.isoformat()}-{due.isoformat()}",
                rule_name="cleaning_payment",
                title="Pay Cleaning Company",
                due_date=due,
            )
        )
    return tasks


# --- Rules 4 & 5: tack-on tasks (Pool always, Air Filter with cooldown) ---


def apply_tackons(base_tasks: list[TaskItem], air_filter_last_fired: date | None) -> list[TaskItem]:
    """Given the tasks produced by the other rules, add:
      - Pool: one 'check pool water level' task per distinct task-day.
      - Air Filter: one 'check air filter' task on the earliest eligible
        task-day that is >= AIR_FILTER_COOLDOWN_DAYS after air_filter_last_fired.
    Returns only the NEW tack-on tasks (caller merges them with base_tasks)."""
    added = []

    task_days = sorted({t.due_date for t in base_tasks})
    if not task_days:
        return added

    # Pool: one per task-day.
    for day in task_days:
        added.append(
            TaskItem(
                key=f"pool-{day.isoformat()}",
                rule_name="pool",
                title="Check pool water level",
                due_date=day,
            )
        )

    # Air Filter: earliest task-day past the cooldown.
    for day in task_days:
        if air_filter_last_fired is None or (day - air_filter_last_fired).days >= AIR_FILTER_COOLDOWN_DAYS:
            added.append(
                TaskItem(
                    key=f"airfilter-{day.isoformat()}",
                    rule_name="air_filter",
                    title="Check air filter",
                    due_date=day,
                )
            )
            break

    return added


# --- Coordinator ---


def evaluate_task_rules(history_rows, air_filter_last_fired: date | None) -> list[TaskItem]:
    """Run all five rules against booking history and return the merged task
    list (restock + grill + cleaning payment + pool + air-filter tack-ons).

    Counters (restock checkouts, grill nights) are computed over the FULL
    history so past stays still count -- but only tasks due today or later are
    actually returned. Cleaning payment is computed and filtered the same way,
    but is kept OUT of the set passed to apply_tackons, so it never causes
    Pool/Air Filter to fire (they'd otherwise trigger on every single
    checkout, which defeats the point of them being tied to maintenance
    tasks specifically)."""
    stays = _rows_to_stays(history_rows)
    today = date.today()

    base = evaluate_restock(stays) + evaluate_grill(stays)
    base = [t for t in base if t.due_date >= today]

    cleaning_payment = [t for t in evaluate_cleaning_payment(stays) if t.due_date >= today]

    tackons = apply_tackons(base, air_filter_last_fired)
    return base + cleaning_payment + tackons
