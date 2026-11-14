"""Tenant fairness: limiter behaviour (Redis mocked) and how run_job uses it."""

import unittest
import uuid
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import redis

from app.core.messages import JobMessage
from app.core.states import Status
from app.db.models import utcnow
from app.pipeline import run
from app.services.tenant_limits import TenantLimiter
from tests.helpers import fake_scope, make_settings

RUN = "app.pipeline.run"


def _limiter(client=None, rate=10):
    return TenantLimiter(client or MagicMock(), rate_per_s=rate, max_concurrent=4, slot_ttl_s=960)


class RateLimitTests(unittest.TestCase):
    def _with_count(self, count, rate=10):
        client = MagicMock()
        client.pipeline.return_value.execute.return_value = [count, True]
        return _limiter(client, rate)

    def test_under_the_limit_is_allowed(self):
        self.assertTrue(self._with_count(10).allow_request("acme"))

    def test_over_the_limit_is_refused(self):
        self.assertFalse(self._with_count(11).allow_request("acme"))

    def test_redis_down_fails_open(self):
        client = MagicMock()
        client.pipeline.return_value.execute.side_effect = redis.ConnectionError()
        self.assertTrue(_limiter(client).allow_request("acme"))


class SlotTests(unittest.TestCase):
    def test_granted_and_refused(self):
        limiter = _limiter()
        limiter._acquire = MagicMock(return_value=1)
        self.assertTrue(limiter.acquire_slot("acme", uuid.uuid4()))
        limiter._acquire = MagicMock(return_value=0)
        self.assertFalse(limiter.acquire_slot("acme", uuid.uuid4()))

    def test_redis_down_fails_open(self):
        limiter = _limiter()
        limiter._acquire = MagicMock(side_effect=redis.ConnectionError())
        self.assertTrue(limiter.acquire_slot("acme", uuid.uuid4()))

    def test_slots_are_per_tenant(self):
        limiter = _limiter()
        limiter._acquire = MagicMock(return_value=1)
        limiter.acquire_slot("acme", uuid.uuid4())
        self.assertEqual(limiter._acquire.call_args.kwargs["keys"], ["slots:acme"])

    def test_release_removes_the_member(self):
        client = MagicMock()
        fid = uuid.uuid4()
        _limiter(client).release_slot("acme", fid)
        client.zrem.assert_called_once_with("slots:acme", str(fid))


class RunJobFairnessTests(unittest.TestCase):
    def setUp(self):
        self.job = JobMessage(file_id=uuid.uuid4(), tenant_id="acme")
        self.info = SimpleNamespace(tenant_id="acme", file_id=self.job.file_id)
        self.deps = SimpleNamespace(settings=make_settings())

    def test_tenant_at_cap_is_deferred_not_processed(self):
        limiter = MagicMock()
        limiter.acquire_slot.return_value = False
        with (
            patch(f"{RUN}._claim", return_value=self.info),
            patch(f"{RUN}.get_limiter", return_value=limiter),
            patch(f"{RUN}._defer") as defer,
            patch(f"{RUN}._process") as process,
        ):
            run.run_job(self.job, self.deps)
        defer.assert_called_once()
        process.assert_not_called()
        limiter.release_slot.assert_not_called()  # nothing was acquired

    def test_slot_is_released_even_when_processing_crashes(self):
        limiter = MagicMock()
        limiter.acquire_slot.return_value = True
        with (
            patch(f"{RUN}._claim", return_value=self.info),
            patch(f"{RUN}.get_limiter", return_value=limiter),
            patch(f"{RUN}._process", side_effect=RuntimeError("boom")),
        ):
            with self.assertRaises(RuntimeError):
                run.run_job(self.job, self.deps)
        limiter.release_slot.assert_called_once_with("acme", self.job.file_id)

    def test_lost_claim_never_touches_the_limiter(self):
        limiter = MagicMock()
        with patch(f"{RUN}._claim", return_value=None), patch(f"{RUN}.get_limiter", return_value=limiter):
            run.run_job(self.job, self.deps)
        limiter.acquire_slot.assert_not_called()
        limiter.release_slot.assert_not_called()


class DeferTests(unittest.TestCase):
    def test_file_goes_back_to_queued_with_a_future_retry_at(self):
        scope, _ = fake_scope()
        job = JobMessage(file_id=uuid.uuid4(), tenant_id="acme")
        with patch(f"{RUN}.session_scope", scope), patch(f"{RUN}.transition") as tr:
            run._defer(job, make_settings(tenant_defer_s=15))
        self.assertEqual(tr.call_args.args[3], Status.PROCESSING)
        self.assertEqual(tr.call_args.args[4], Status.QUEUED)
        wait = tr.call_args.kwargs["retry_at"] - utcnow()
        self.assertTrue(timedelta(seconds=10) < wait <= timedelta(seconds=30))
        self.assertIn("attempts", tr.call_args.kwargs)  # the counted attempt is given back


if __name__ == "__main__":
    unittest.main()