"""The reconciler rules, with the database, storage and broker mocked."""

import unittest
import uuid
from unittest.mock import MagicMock, patch

from app.core.states import Status
from app.maintenance import reconciler
from tests.helpers import fake_scope, make_settings

REC = "app.maintenance.reconciler"


class AdvanceUploadedTests(unittest.TestCase):
    def test_stuck_uploaded_file_is_queued_on_the_right_queue(self):
        scope, db = fake_scope()
        db.execute.return_value.all.return_value = [("acme", uuid.uuid4(), 1000)]
        with patch(f"{REC}.session_scope", scope), patch(f"{REC}.transition", return_value=True) as tr:
            moved = reconciler._advance_uploaded(make_settings(), reconciler.utcnow())
        self.assertEqual(moved, 1)
        self.assertEqual(tr.call_args.args[3], Status.UPLOADED)
        self.assertEqual(tr.call_args.args[4], Status.QUEUED)
        self.assertEqual(tr.call_args.kwargs["queue"], "small")

    def test_lost_race_is_not_counted(self):
        scope, db = fake_scope()
        db.execute.return_value.all.return_value = [("acme", uuid.uuid4(), 1000)]
        with patch(f"{REC}.session_scope", scope), patch(f"{REC}.transition", return_value=False):
            self.assertEqual(reconciler._advance_uploaded(make_settings(), reconciler.utcnow()), 0)


class ReapProcessingTests(unittest.TestCase):
    def test_requeue_within_budget_and_fail_beyond_it(self):
        scope, db = fake_scope()
        db.execute.return_value.all.return_value = [
            ("acme", uuid.uuid4(), 2),  # still has attempts left
            ("acme", uuid.uuid4(), 5),  # budget used up
        ]
        with patch(f"{REC}.session_scope", scope), patch(f"{REC}.transition", return_value=True) as tr:
            requeued, failed = reconciler._reap_processing(make_settings(), reconciler.utcnow())
        self.assertEqual((requeued, failed), (1, 1))
        destinations = [call.args[4] for call in tr.call_args_list]
        self.assertEqual(destinations, [Status.QUEUED, Status.FAILED])
        self.assertEqual(tr.call_args_list[1].kwargs["error_code"], "WORKER_LOST")


class PublishDueTests(unittest.TestCase):
    def _run(self, rows, head_value, publish):
        scope, db = fake_scope()
        db.execute.return_value.all.return_value = rows
        storage = MagicMock()
        storage.head.return_value = head_value
        with patch(f"{REC}.session_scope", scope), patch(f"{REC}.transition", return_value=True) as tr:
            result = reconciler._publish_due(make_settings(), storage, publish)
        return result, tr

    def test_due_file_is_published_to_its_queue(self):
        fid = uuid.uuid4()
        publish = MagicMock()
        result, _ = self._run([("acme", fid, "large", 50, "k")], {"ContentLength": 50}, publish)
        self.assertEqual(result, (1, 0))
        message, queue = publish.call_args.args
        self.assertEqual(message.file_id, fid)
        self.assertEqual(queue, "large")

    def test_missing_original_fails_the_file_and_nothing_is_published(self):
        publish = MagicMock()
        result, tr = self._run([("acme", uuid.uuid4(), "small", 50, "k")], None, publish)
        self.assertEqual(result, (0, 1))
        publish.assert_not_called()
        self.assertEqual(tr.call_args.args[4], Status.FAILED)
        self.assertEqual(tr.call_args.kwargs["error_code"], "ORIGINAL_MISSING")

    def test_broker_failure_does_not_stop_the_cycle(self):
        publish = MagicMock(side_effect=ConnectionError("broker down"))
        rows = [("acme", uuid.uuid4(), "small", 1, "k1"), ("acme", uuid.uuid4(), "small", 1, "k2")]
        result, _ = self._run(rows, {"ContentLength": 1}, publish)
        self.assertEqual(result, (0, 0))
        self.assertEqual(publish.call_count, 2)  # the second file was still attempted


class ReconcileOnceTests(unittest.TestCase):
    def test_collects_stats_from_all_rules(self):
        with (
            patch(f"{REC}._advance_uploaded", return_value=3),
            patch(f"{REC}._reap_processing", return_value=(2, 1)),
            patch(f"{REC}._publish_due", return_value=(4, 0)),
        ):
            stats = reconciler.reconcile_once(make_settings(), MagicMock(), MagicMock())
        self.assertEqual(
            stats,
            {
                "advanced_uploaded": 3,
                "reaped_requeued": 2,
                "reaped_failed": 1,
                "published": 4,
                "failed_original_missing": 0,
            },
        )


if __name__ == "__main__":
    unittest.main()