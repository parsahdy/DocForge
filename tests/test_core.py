"""Pure logic: no database, no network."""

import random
import time
import unittest
import uuid
from unittest.mock import MagicMock

from app.core.config import MB
from app.core.errors import PermanentError
from app.core.ids import chunk_id, uuid7
from app.core.keys import chunks_key, original_key, text_key
from app.core.messages import JobMessage
from app.core.retry import backoff_delay
from app.core.routing import QUEUE_LARGE, QUEUE_SMALL, route_queue
from app.core.states import ALLOWED, IllegalTransition, Status, check_transition
from app.db.transitions import transition


class BackoffTests(unittest.TestCase):
    def test_delay_never_exceeds_the_ceiling(self):
        rng = random.Random(1)
        for attempt, ceiling in [(1, 10), (2, 20), (3, 40), (4, 80)]:
            for _ in range(200):
                self.assertTrue(0 <= backoff_delay(attempt, 10, 300, rng) <= ceiling)

    def test_ceiling_is_capped(self):
        rng = random.Random(2)
        for _ in range(200):
            self.assertLessEqual(backoff_delay(20, 10, 300, rng), 300)

    def test_jitter_spreads_the_delays(self):
        rng = random.Random(3)
        delays = {round(backoff_delay(5, 10, 300, rng), 3) for _ in range(50)}
        self.assertGreater(len(delays), 40)  # not all files retry at the same moment


class RoutingTests(unittest.TestCase):
    def test_boundary_is_inclusive_for_small(self):
        self.assertEqual(route_queue(25 * MB, 25), QUEUE_SMALL)
        self.assertEqual(route_queue(25 * MB + 1, 25), QUEUE_LARGE)

    def test_tiny_file_is_small(self):
        self.assertEqual(route_queue(1, 25), QUEUE_SMALL)


class StateMachineTests(unittest.TestCase):
    def test_every_status_has_a_rule(self):
        self.assertEqual(set(ALLOWED), set(Status))

    def test_happy_path_is_allowed(self):
        path = [Status.PENDING_UPLOAD, Status.UPLOADED, Status.QUEUED, Status.PROCESSING, Status.READY]
        for src, dst in zip(path, path[1:]):
            check_transition(src, dst)

    def test_retry_and_manual_retry_paths(self):
        check_transition(Status.PROCESSING, Status.QUEUED)
        check_transition(Status.FAILED, Status.QUEUED)

    def test_ready_is_terminal(self):
        for dst in Status:
            with self.assertRaises(IllegalTransition):
                check_transition(Status.READY, dst)

    def test_skipping_a_state_is_illegal(self):
        with self.assertRaises(IllegalTransition):
            check_transition(Status.UPLOADED, Status.PROCESSING)


class CompareAndSetTests(unittest.TestCase):
    def test_returns_true_when_one_row_changed(self):
        db = MagicMock()
        db.execute.return_value.rowcount = 1
        self.assertTrue(transition(db, "acme", uuid.uuid4(), Status.QUEUED, Status.PROCESSING))

    def test_returns_false_when_someone_else_won(self):
        db = MagicMock()
        db.execute.return_value.rowcount = 0
        self.assertFalse(transition(db, "acme", uuid.uuid4(), Status.QUEUED, Status.PROCESSING))

    def test_illegal_transition_never_reaches_the_database(self):
        db = MagicMock()
        with self.assertRaises(IllegalTransition):
            transition(db, "acme", uuid.uuid4(), Status.READY, Status.QUEUED)
        db.execute.assert_not_called()


class MessageTests(unittest.TestCase):
    def test_roundtrip(self):
        msg = JobMessage(file_id=uuid.uuid4(), tenant_id="acme")
        parsed = JobMessage.parse(msg.to_wire())
        self.assertEqual(parsed, msg)

    def test_unknown_extra_fields_are_ignored(self):
        raw = JobMessage(file_id=uuid.uuid4(), tenant_id="acme").to_wire() | {"future_field": 1}
        self.assertEqual(JobMessage.parse(raw).tenant_id, "acme")

    def test_unsupported_version_is_permanent(self):
        raw = JobMessage(file_id=uuid.uuid4(), tenant_id="acme").to_wire() | {"v": 99}
        with self.assertRaises(PermanentError) as ctx:
            JobMessage.parse(raw)
        self.assertEqual(ctx.exception.code, "UNSUPPORTED_MESSAGE_VERSION")

    def test_malformed_message_is_permanent(self):
        with self.assertRaises(PermanentError) as ctx:
            JobMessage.parse({"v": 1, "file_id": "not-a-uuid", "tenant_id": "acme"})
        self.assertEqual(ctx.exception.code, "BAD_MESSAGE")


class KeyLayoutTests(unittest.TestCase):
    def test_tenant_comes_first_and_filename_is_absent(self):
        fid = uuid.uuid4()
        self.assertEqual(original_key("acme", fid), f"t/acme/f/{fid}/original")
        self.assertEqual(text_key("acme", fid), f"t/acme/f/{fid}/text.jsonl.gz")
        self.assertEqual(chunks_key("acme", fid, "v1"), f"t/acme/f/{fid}/chunks.v1.jsonl.gz")


class IdTests(unittest.TestCase):
    def test_uuid7_is_version_7_and_time_ordered(self):
        first = uuid7()
        time.sleep(0.005)
        second = uuid7()
        self.assertEqual(first.version, 7)
        self.assertLess(first, second)

    def test_chunk_id_is_deterministic(self):
        fid = uuid.uuid4()
        self.assertEqual(chunk_id("acme", fid, "v1", 0), chunk_id("acme", fid, "v1", 0))

    def test_chunk_id_differs_by_tenant_version_and_index(self):
        fid = uuid.uuid4()
        base = chunk_id("acme", fid, "v1", 0)
        self.assertNotEqual(base, chunk_id("other", fid, "v1", 0))  # two tenants never overwrite each other
        self.assertNotEqual(base, chunk_id("acme", fid, "v2", 0))
        self.assertNotEqual(base, chunk_id("acme", fid, "v1", 1))


if __name__ == "__main__":
    unittest.main()