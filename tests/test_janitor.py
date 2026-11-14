"""Lifecycle policy and orphan audit, with storage and the database mocked."""

import unittest
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from app.maintenance import janitor
from tests.helpers import fake_scope, make_settings

JAN = "app.maintenance.janitor"


class ParseKeyTests(unittest.TestCase):
    def test_valid_key(self):
        fid = uuid.uuid4()
        self.assertEqual(janitor._parse_key(f"t/acme/f/{fid}/original"), ("acme", fid))

    def test_foreign_or_malformed_keys_are_ignored(self):
        self.assertIsNone(janitor._parse_key("other/thing"))
        self.assertIsNone(janitor._parse_key("t/acme/f/not-a-uuid/original"))
        self.assertIsNone(janitor._parse_key("t/acme/x/" + str(uuid.uuid4()) + "/original"))


class ExpirePendingTests(unittest.TestCase):
    def test_object_is_deleted_then_record(self):
        scope, db = fake_scope()
        db.execute.return_value.all.return_value = [("acme", uuid.uuid4(), "key-1")]
        storage = MagicMock()
        with patch(f"{JAN}.session_scope", scope):
            count = janitor.expire_pending_uploads(make_settings(), storage)
        self.assertEqual(count, 1)
        storage.delete.assert_called_once_with("key-1")


class RetentionTests(unittest.TestCase):
    def test_expired_originals_are_deleted_and_marked(self):
        scope, db = fake_scope()
        db.execute.return_value.all.return_value = [
            ("acme", uuid.uuid4(), "k1"),
            ("acme", uuid.uuid4(), "k2"),
        ]
        storage = MagicMock()
        with patch(f"{JAN}.session_scope", scope):
            count = janitor.apply_retention(make_settings(), storage)
        self.assertEqual(count, 2)
        self.assertEqual([c.args[0] for c in storage.delete.call_args_list], ["k1", "k2"])


class OrphanAuditTests(unittest.TestCase):
    def _objects(self):
        old = datetime.now(timezone.utc) - timedelta(days=2)
        fresh = datetime.now(timezone.utc)
        orphan = f"t/acme/f/{uuid.uuid4()}/original"
        fresh_key = f"t/acme/f/{uuid.uuid4()}/original"
        return orphan, fresh_key, [
            {"Key": orphan, "LastModified": old},
            {"Key": fresh_key, "LastModified": fresh},  # inside the grace period
            {"Key": "foreign/key", "LastModified": old},  # not ours
        ]

    def test_dry_run_reports_but_does_not_delete(self):
        orphan, _, objects = self._objects()
        storage = MagicMock()
        storage.iter_objects.return_value = iter(objects)
        with patch(f"{JAN}._keys_without_record", side_effect=lambda page: [p[2] for p in page]):
            found = janitor.find_orphan_objects(make_settings(), storage, delete_found=False)
        self.assertEqual(found, [orphan])  # fresh and foreign objects are skipped
        storage.delete.assert_not_called()

    def test_delete_mode_removes_what_it_found(self):
        orphan, _, objects = self._objects()
        storage = MagicMock()
        storage.iter_objects.return_value = iter(objects)
        with patch(f"{JAN}._keys_without_record", side_effect=lambda page: [p[2] for p in page]):
            janitor.find_orphan_objects(make_settings(), storage, delete_found=True)
        storage.delete.assert_called_once_with(orphan)

    def test_known_files_are_not_orphans(self):
        known, orphan = uuid.uuid4(), uuid.uuid4()
        page = [
            ("acme", known, f"t/acme/f/{known}/original"),
            ("acme", orphan, f"t/acme/f/{orphan}/original"),
        ]
        scope, db = fake_scope()
        db.execute.return_value.all.return_value = [("acme", known)]
        with patch(f"{JAN}.session_scope", scope):
            keys = janitor._keys_without_record(page)
        self.assertEqual(keys, [f"t/acme/f/{orphan}/original"])


if __name__ == "__main__":
    unittest.main()