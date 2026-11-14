"""The claim (compare-and-set) and the retry-or-give-up decision."""

import unittest
import uuid
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from app.core.errors import PermanentError, TransientError
from app.core.messages import JobMessage
from app.core.states import Status
from app.db.models import utcnow
from app.pipeline import run
from tests.helpers import fake_scope, make_settings

RUN = "app.pipeline.run"


def _job():
    return JobMessage(file_id=uuid.uuid4(), tenant_id="acme")


class HandleFailureTests(unittest.TestCase):
    def _run(self, exc, attempts):
        scope, db = fake_scope()
        db.get.return_value = SimpleNamespace(attempts=attempts)
        with (
            patch(f"{RUN}.session_scope", scope),
            patch(f"{RUN}.transition") as tr,
            patch(f"{RUN}.backoff_delay", return_value=7.0) as backoff,
        ):
            run._handle_failure(_job(), exc, make_settings())
        return tr, backoff

    def test_permanent_error_fails_immediately_without_retry(self):
        tr, backoff = self._run(PermanentError("TYPE_MISMATCH", "bad file"), attempts=1)
        backoff.assert_not_called()
        self.assertEqual(tr.call_args.args[4], Status.FAILED)
        self.assertEqual(tr.call_args.kwargs["error_code"], "TYPE_MISMATCH")
        self.assertIn("bad file", tr.call_args.kwargs["error_message"])

    def test_transient_error_within_budget_is_requeued_with_retry_at(self):
        tr, backoff = self._run(TransientError("EMBED_FAILED", "timeout"), attempts=2)
        backoff.assert_called_once()
        self.assertEqual(tr.call_args.args[3], Status.PROCESSING)
        self.assertEqual(tr.call_args.args[4], Status.QUEUED)
        wait = tr.call_args.kwargs["retry_at"] - utcnow()
        self.assertTrue(timedelta(seconds=6) < wait <= timedelta(seconds=7))

    def test_transient_error_with_exhausted_budget_fails(self):
        tr, _ = self._run(TransientError("EMBED_FAILED", "timeout"), attempts=5)
        self.assertEqual(tr.call_args.args[4], Status.FAILED)
        self.assertTrue(tr.call_args.kwargs["error_message"].startswith("gave up after 5 attempts"))

    def test_unknown_exception_is_recorded_and_retried(self):
        tr, _ = self._run(RuntimeError("boom"), attempts=1)
        self.assertEqual(tr.call_args.args[4], Status.QUEUED)
        self.assertEqual(tr.call_args.kwargs["error_code"], "INTERNAL_ERROR")


class ClaimTests(unittest.TestCase):
    def test_winner_gets_file_info(self):
        scope, db = fake_scope()
        db.get.return_value = SimpleNamespace(filename="a.txt", s3_key="t/acme/f/x/original", status="PROCESSING")
        with patch(f"{RUN}.session_scope", scope), patch(f"{RUN}.transition", return_value=True) as tr:
            info = run._claim(_job())
        self.assertIsNotNone(info)
        self.assertEqual(info.filename, "a.txt")
        self.assertEqual(tr.call_args.args[3], Status.QUEUED)
        self.assertEqual(tr.call_args.args[4], Status.PROCESSING)

    def test_loser_gets_none_so_duplicate_messages_are_dropped(self):
        scope, db = fake_scope()
        db.get.return_value = SimpleNamespace(filename="a.txt", s3_key="k", status="PROCESSING")
        with patch(f"{RUN}.session_scope", scope), patch(f"{RUN}.transition", return_value=False):
            self.assertIsNone(run._claim(_job()))

    def test_missing_row_gets_none(self):
        scope, db = fake_scope()
        db.get.return_value = None
        with patch(f"{RUN}.session_scope", scope), patch(f"{RUN}.transition", return_value=True):
            self.assertIsNone(run._claim(_job()))


if __name__ == "__main__":
    unittest.main()