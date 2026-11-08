"""Retention rules (Task 15.6). Pure functions, so the policy is testable without touching a file.

Backups: keep the newest ``daily`` days of backups (one per day) and the newest ``weekly`` Sunday backups.
Anything not matching the Atlas backup file name is never selected for deletion.
"""

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

BACKUP_NAME = re.compile(r"^atlas-pg-(\d{8}T\d{6}Z)\.dump\.enc$")


@dataclass(frozen=True)
class RetentionPolicy:
    daily: int = 7
    weekly: int = 4


def backup_time(name: str) -> datetime | None:
    m = BACKUP_NAME.match(name)
    return datetime.strptime(m.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC) if m else None


def plan_backup_retention(names: list[str], now: datetime, policy: RetentionPolicy | None = None
                          ) -> tuple[list[str], list[str]]:
    """``(keep, delete)``. Foreign files are in neither list. The newest backup is always kept."""
    policy = policy or RetentionPolicy()
    dated = sorted(((backup_time(n), n) for n in names if backup_time(n)), reverse=True)
    if not dated:
        return [], []
    keep: set[str] = {dated[0][1]}
    by_day: dict[str, str] = {}
    for when, name in dated:  # newest first: the first backup seen for a day is that day's latest
        by_day.setdefault(when.strftime("%Y-%m-%d"), name)
    for day in sorted(by_day, reverse=True)[: policy.daily]:
        keep.add(by_day[day])
    weekly = [by_day[d] for d in sorted(by_day, reverse=True)
              if datetime.strptime(d, "%Y-%m-%d").weekday() == 6]  # Sundays
    keep.update(weekly[: policy.weekly])
    del now  # retention depends on the backups present, not on the clock: a stopped job never loses its last backups
    return sorted(keep), sorted(n for _, n in dated if n not in keep)


def is_stale(when: datetime, now: datetime, max_age: timedelta) -> bool:
    return now - when > max_age
