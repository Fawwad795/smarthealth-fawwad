"""Compare analytics_daily against the raw tables, and optionally repair it.

Run with:
    docker compose exec api python -m scripts.reconcile_analytics
    docker compose exec api python -m scripts.reconcile_analytics --days 30
    docker compose exec api python -m scripts.reconcile_analytics --repair

The aggregates are maintained one event at a time by the Kafka consumer,
which is fast but never re-checks its own arithmetic: a missed event or a
double-counted one stays wrong forever, because nothing looks at the raw
tables again. This is the thing that looks.

Exits 1 when it finds drift it did not repair, so cron or CI can act on
the result. A script that always exits 0 cannot be alerted on, and an
unwatched check is the same as no check.
"""

import argparse
from datetime import UTC, datetime, timedelta

from app.db.session import SessionLocal
from app.services import analytics as analytics_service

# How far back to look when nothing is asked for. Long enough to cover a
# quiet weekend plus the week around it.
DEFAULT_DAYS = 14


def main() -> None:
    """Parse the arguments, run the check, print what it found."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--days",
        type=int,
        default=DEFAULT_DAYS,
        help=f"How many days back to check, ending today (default {DEFAULT_DAYS}).",
    )
    parser.add_argument(
        "--repair",
        action="store_true",
        help=(
            "Rewrite every drifted day from the raw tables. Never automatic: "
            "an aggregate that heals itself hides the bug that broke it."
        ),
    )
    args = parser.parse_args()

    # UTC, matching the buckets the handlers write. Using the machine's
    # local date here would shift the window by a day for half the world
    # and report drift that was never real.
    end_date = datetime.now(UTC).date()
    start_date = end_date - timedelta(days=args.days - 1)

    db = SessionLocal()
    try:
        drifted = analytics_service.reconcile_range(db, start_date, end_date)

        if not drifted:
            print(f"In sync: {start_date} to {end_date}, no drift found.")
            return

        print(f"Drift found: {start_date} to {end_date}, {len(drifted)} day(s).")
        for day in drifted:
            print(f"  {day['date']}")
            for field, values in day["fields"].items():
                print(
                    f"    {field}: stored={values['stored']} "
                    f"actual={values['actual']}"
                )

        if not args.repair:
            print("\nRe-run with --repair to rewrite these days from the raw tables.")
            raise SystemExit(1)

        for day in drifted:
            analytics_service.repair_date(db, day["date"])
            print(f"  repaired {day['date']}")
        print(f"\nRepaired {len(drifted)} day(s).")
    finally:
        db.close()


if __name__ == "__main__":
    main()
