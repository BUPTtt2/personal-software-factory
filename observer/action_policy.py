from __future__ import annotations


CURRENT_ACTION_STATUSES = frozenset({"proposed", "ready", "in_progress"})
CURRENT_ACTION_STATUS_SQL = "(" + ",".join(
    f"'{status}'" for status in sorted(CURRENT_ACTION_STATUSES)
) + ")"


def is_current_action_status(status: str) -> bool:
    return status in CURRENT_ACTION_STATUSES
