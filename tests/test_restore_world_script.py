import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, call
from urllib.error import URLError

from scripts.restore_world import recover


class RestoreWorldScriptTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.marker = Path(directory.name) / "world.pending"
        self.closed = (409, {"detail": {"category": "world_not_open"}})
        self.opened = (200, {"world_id": "world", "revision": 42})
        self.probe = call("GET", "/overview?limit=1", 15)
        self.restore = call("POST", "/restore", 900)

    def test_verifies_success_after_one_restore(self):
        api = Mock(side_effect=[self.closed, (200, {"revision": 42, "running": True}), self.opened])
        self.assertEqual(recover(api, "world", self.marker, 900), 0)
        self.assertEqual(api.call_args_list, [
            self.probe, self.restore, call("GET", "/overview?limit=1", 60),
        ])
        self.assertFalse(self.marker.exists())

    def test_timeout_blocks_resubmission_until_overview_confirms_open(self):
        api = Mock(side_effect=[self.closed, TimeoutError(), self.closed, self.opened])
        self.assertEqual(recover(api, "world", self.marker, 900), 2)
        self.assertTrue(self.marker.exists())
        self.assertEqual(recover(api, "world", self.marker, 900), 2)
        self.assertEqual(recover(api, "world", self.marker, 900), 0)
        self.assertEqual(api.call_args_list, [self.probe, self.restore, self.probe, self.probe])
        self.assertFalse(self.marker.exists())

    def test_200_alone_does_not_verify_restore(self):
        api = Mock(side_effect=[self.closed, (200, {"revision": 42, "running": True}), self.closed])
        self.assertEqual(recover(api, "world", self.marker, 900), 2)
        self.assertTrue(self.marker.exists())

    def test_unexpected_preflight_never_submits_restore(self):
        api = Mock(return_value=(503, {}))
        self.assertEqual(recover(api, "world", self.marker, 900), 1)
        api.assert_called_once_with("GET", "/overview?limit=1", 15)
        self.assertFalse(self.marker.exists())

    def test_definitive_content_refusal_allows_later_attempt(self):
        rejection = (409, {"detail": {"category": "content_not_adopted"}})
        api = Mock(side_effect=[self.closed, rejection, self.closed, (400, {"detail": {"category": "invalid_content"}})])
        for _ in range(2):
            self.assertEqual(recover(api, "world", self.marker, 900), 1)
            self.assertFalse(self.marker.exists())
        self.assertEqual(api.call_args_list, [self.probe, self.restore] * 2)

    def test_connection_refused_does_not_leave_pending(self):
        api = Mock(side_effect=[self.closed, URLError(ConnectionRefusedError())])
        self.assertEqual(recover(api, "world", self.marker, 900), 1)
        self.assertFalse(self.marker.exists())

    def test_busy_overview_preserves_pending_and_uncertain_exit_code(self):
        self.marker.touch()
        api = Mock(return_value=(503, {"detail": {"category": "world_busy"}}))
        self.assertEqual(recover(api, "world", self.marker, 900), 2)
        self.assertTrue(self.marker.exists())
        api.assert_called_once_with("GET", "/overview?limit=1", 15)
