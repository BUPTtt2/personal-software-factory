import unittest
from datetime import datetime, timedelta, timezone

from observer.status import TurnEvidence, derive_turn_status


NOW = datetime(2026, 8, 13, 12, tzinfo=timezone.utc)


class StatusTests(unittest.TestCase):
    def derive(self, **changes):
        values = dict(
            started_at=NOW - timedelta(minutes=1), stopped=False, thread_runtime="active",
            changes_detected=False, verification_statuses=(), awaiting_user=False,
            assistant_claim_summary="全部完成",
        )
        values.update(changes)
        return derive_turn_status(TurnEvidence(**values), NOW, timedelta(minutes=30))

    def test_truth_table(self):
        self.assertEqual(self.derive().status, "working")
        self.assertEqual(self.derive(started_at=NOW - timedelta(hours=1), thread_runtime="idle").status, "interrupted")
        self.assertEqual(self.derive(started_at=NOW - timedelta(hours=1), thread_runtime="unknown").status, "unknown")
        self.assertEqual(self.derive(stopped=True, thread_runtime="idle").status, "stopped")
        self.assertEqual(self.derive(stopped=True, changes_detected=True).status, "evidence_incomplete")
        self.assertEqual(self.derive(stopped=True, changes_detected=True, verification_statuses=("passed",)).status, "verification_passed")
        self.assertEqual(self.derive(verification_statuses=("failed",)).status, "verification_failed")
        self.assertEqual(self.derive(awaiting_user=True).status, "awaiting_user")

    def test_claim_never_changes_status(self):
        without = self.derive(assistant_claim_summary="")
        with_claim = self.derive(assistant_claim_summary="全部完成并已上线")
        self.assertEqual(without, with_claim)


if __name__ == "__main__":
    unittest.main()
