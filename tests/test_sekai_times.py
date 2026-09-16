import io
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse

from pns.integrations.sekai_times import (
    DeliveryError,
    DeliveryRejected,
    DeliveryUncertain,
    PendingPost,
    PendingPostOutbox,
    SubmissionError,
    WordPressPendingPosts,
)
from pns.models.authored import GenerationAudit


class Response(io.BytesIO):
    def __init__(self, value, status=200):
        super().__init__(json.dumps(value, ensure_ascii=False).encode())
        self.status = status


class FakeWordPress:
    def __init__(self):
        self.posts = []
        self.calls = []
        self.lost_response = False
        self.reject = None
        self.reject_after_create = None
        self.category_name = "暁山瑞希"
        self.on_post = None
        self.contract_has_submission_id = True

    def __call__(self, request, timeout):
        self.calls.append(request)
        parsed = urlparse(request.full_url)
        query = parse_qs(parsed.query)
        if parsed.path == "/wp-json/":
            properties = {"pns_submission_id": {"type": "string"}}
            if not self.contract_has_submission_id:
                properties = {}
            return Response(
                {
                    "routes": {
                        "/wp/v2/posts": {
                            "endpoints": [
                                {
                                    "methods": ["GET"],
                                    "args": {},
                                },
                                {
                                    "methods": ["POST"],
                                    "args": {"meta": {"properties": properties}},
                                },
                            ]
                        }
                    }
                }
            )
        if parsed.path.endswith("/categories"):
            return Response([{"id": 99, "slug": "mzk", "name": self.category_name}])
        if request.get_method() == "GET":
            posts = [
                post for post in self.posts if post["status"] == query["status"][0]
            ]
            posts = posts[
                int(query.get("offset", [0])[0]) : int(query.get("offset", [0])[0])
                + int(query.get("per_page", [100])[0])
            ]
            return Response(posts)
        if self.reject:
            raise HTTPError(request.full_url, self.reject, "rejected", {}, None)
        payload = json.loads(request.data)
        if self.on_post:
            self.on_post()
        created = {**payload, "id": len(self.posts) + 1, "slug": ""}
        self.posts.append(created)
        if self.reject_after_create:
            code = self.reject_after_create
            self.reject_after_create = None
            raise HTTPError(request.full_url, code, "error after creation", {}, None)
        if self.lost_response:
            self.lost_response = False
            raise URLError("connection reset")
        return Response(created, 201)


def post(**overrides):
    data = dict(
        world_id="yoake-mae",
        source_id="accepted-post-action-1",
        session_id="session-1",
        character_id="mizuki",
        title="今日のこと",
        content="今日は色々あった。",
        drift_score=2.0,
        router_confidence=0.9,
    )
    data.update(overrides)
    return PendingPost(**data)


def audit_for(value, **overrides):
    data = dict(
        proposal_id=value.source_id,
        character_id=value.character_id,
        payload={"text": value.content, "title": value.title},
        drift_score=value.drift_score,
        threshold=5.0,
        audited_at=datetime(2026, 9, 16, 19, 0),
        confidence=value.router_confidence,
    )
    data.update(overrides)
    return GenerationAudit(**data)


class SekaiTimesTests(unittest.TestCase):
    def test_pending_payload_uses_verified_category_and_never_publish(self):
        server = FakeWordPress()
        client = WordPressPendingPosts(
            "http://127.0.0.1:8090", "pns_bot", "test", opener=server
        )
        created_id = client.submit_once(post())
        self.assertEqual(created_id, 1)
        self.assertEqual(server.posts[0]["status"], "pending")
        self.assertEqual(server.posts[0]["categories"], [99])
        self.assertEqual(server.posts[0]["meta"]["character_jp"], "暁山瑞希")
        self.assertEqual(server.posts[0]["meta"]["interaction_type"], "blog_post")
        self.assertEqual(server.posts[0]["meta"]["pns_submission_id"], post().slug)
        self.assertEqual(server.posts[0]["slug"], "")
        self.assertEqual(client.submit_once(post()), 1)
        self.assertEqual(len(server.posts), 1)
        self.assertTrue(
            all(
                call.get_header("Authorization").startswith("Basic ")
                for call in server.calls
            )
        )

    def test_category_mismatch_prevents_write(self):
        server = FakeWordPress()
        server.category_name = "瑞希"
        client = WordPressPendingPosts(
            "http://localhost:8090", "pns_bot", "test", opener=server
        )
        with self.assertRaises(DeliveryError):
            client.submit_once(post())
        self.assertEqual(server.posts, [])

    def test_missing_remote_submission_contract_prevents_write(self):
        server = FakeWordPress()
        server.contract_has_submission_id = False
        client = WordPressPendingPosts(
            "http://localhost:8090", "pns_bot", "test", opener=server
        )
        with self.assertRaises(DeliveryError):
            client.submit_once(post())
        self.assertEqual(server.posts, [])
        self.assertFalse(any(call.get_method() == "POST" for call in server.calls))

    def test_lost_response_is_reconciled_without_second_post(self):
        server = FakeWordPress()
        server.lost_response = True
        client = WordPressPendingPosts(
            "http://127.0.0.1:8090", "pns_bot", "test", opener=server
        )
        with tempfile.TemporaryDirectory() as directory:
            outbox = PendingPostOutbox(Path(directory) / "posts.sqlite3")
            outbox.enqueue(post(), audit_for(post()))
            with self.assertRaises(DeliveryUncertain):
                outbox.dispatch(post().slug, client)
            outbox.close()
            reopened = PendingPostOutbox(Path(directory) / "posts.sqlite3")
            self.assertEqual(reopened.dispatch(post().slug, client), 1)
            self.assertEqual(reopened.dispatch(post().slug, client), 1)
            self.assertEqual(len(server.posts), 1)
            reopened.close()

    def test_unknown_without_remote_record_is_not_resent(self):
        server = FakeWordPress()
        server.reject = 503
        client = WordPressPendingPosts(
            "http://127.0.0.1:8090", "pns_bot", "test", opener=server
        )
        with tempfile.TemporaryDirectory() as directory:
            outbox = PendingPostOutbox(Path(directory) / "posts.sqlite3")
            outbox.enqueue(post(), audit_for(post()))
            with self.assertRaises(DeliveryUncertain):
                outbox.dispatch(post().slug, client)
            server.reject = None
            with self.assertRaises(DeliveryUncertain):
                outbox.dispatch(post().slug, client)
            self.assertEqual(server.posts, [])
            outbox.close()

    def test_redirect_after_post_is_treated_as_uncertain(self):
        server = FakeWordPress()
        server.reject = 302
        client = WordPressPendingPosts(
            "http://127.0.0.1:8090", "pns_bot", "test", opener=server
        )
        with self.assertRaises(DeliveryUncertain):
            client.submit_once(post())

    def test_4xx_without_remote_marker_blocks_and_content_change_refused(self):
        server = FakeWordPress()
        server.reject = 403
        client = WordPressPendingPosts(
            "http://127.0.0.1:8090", "pns_bot", "test", opener=server
        )
        with tempfile.TemporaryDirectory() as directory:
            outbox = PendingPostOutbox(Path(directory) / "posts.sqlite3")
            outbox.enqueue(post(), audit_for(post()))
            with self.assertRaises(SubmissionError):
                outbox.enqueue(
                    post(content="別の本文"), audit_for(post(content="別の本文"))
                )
            with self.assertRaises(DeliveryRejected):
                outbox.dispatch(post().slug, client)
            with self.assertRaises(DeliveryError):
                outbox.dispatch(post().slug, client)
            outbox.close()

    def test_4xx_after_remote_creation_is_reconciled_without_second_post(self):
        server = FakeWordPress()
        server.reject_after_create = 400
        client = WordPressPendingPosts(
            "http://127.0.0.1:8090", "pns_bot", "test", opener=server
        )
        with tempfile.TemporaryDirectory() as directory:
            outbox = PendingPostOutbox(Path(directory) / "posts.sqlite3")
            outbox.enqueue(post(), audit_for(post()))
            self.assertEqual(outbox.dispatch(post().slug, client), 1)
            self.assertEqual(outbox.dispatch(post().slug, client), 1)
            self.assertEqual(len(server.posts), 1)
            outbox.close()

    def test_operator_can_release_blocked_item_with_recorded_resolution(self):
        server = FakeWordPress()
        server.reject = 403
        client = WordPressPendingPosts(
            "http://127.0.0.1:8090", "pns_bot", "test", opener=server
        )
        with tempfile.TemporaryDirectory() as directory:
            outbox = PendingPostOutbox(Path(directory) / "posts.sqlite3")
            outbox.enqueue(post(), audit_for(post()))
            with self.assertRaises(DeliveryRejected):
                outbox.dispatch(post().slug, client)
            with self.assertRaises(SubmissionError):
                outbox.release_for_retry(post().slug, client, resolution="")
            server.reject = None
            self.assertIsNone(
                outbox.release_for_retry(
                    post().slug,
                    client,
                    resolution="local credential was repaired; remote post confirmed absent",
                )
            )
            self.assertEqual(outbox.dispatch(post().slug, client), 1)
            row = outbox.db.execute(
                "SELECT state,resolution FROM pending_posts WHERE slug=?",
                (post().slug,),
            ).fetchone()
            self.assertEqual(row[0], "sent")
            self.assertIn("credential was repaired", row[1])
            outbox.close()

    def test_existing_outbox_schema_gains_resolution_column(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "posts.sqlite3"
            db = sqlite3.connect(path)
            try:
                db.execute(
                    "CREATE TABLE pending_posts ("
                    "slug TEXT PRIMARY KEY, envelope TEXT NOT NULL, "
                    "state TEXT NOT NULL, wp_post_id INTEGER)"
                )
                db.commit()
            finally:
                db.close()
            outbox = PendingPostOutbox(path)
            columns = {
                row[1] for row in outbox.db.execute("PRAGMA table_info(pending_posts)")
            }
            self.assertIn("resolution", columns)
            outbox.close()

    def test_second_dispatcher_cannot_post_while_first_is_in_flight(self):
        server = FakeWordPress()
        client = WordPressPendingPosts(
            "http://127.0.0.1:8090", "pns_bot", "test", opener=server
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "posts.sqlite3"
            first = PendingPostOutbox(path)
            second = PendingPostOutbox(path)
            first.enqueue(post(), audit_for(post()))

            def race():
                with self.assertRaises(DeliveryUncertain):
                    second.dispatch(post().slug, client)

            server.on_post = race
            self.assertEqual(first.dispatch(post().slug, client), 1)
            self.assertEqual(len(server.posts), 1)
            first.close()
            second.close()

    def test_https_required_outside_loopback_and_bad_scores_rejected(self):
        with self.assertRaises(SubmissionError):
            WordPressPendingPosts("http://25nightcord.com", "pns_bot", "test")
        with self.assertRaises(SubmissionError):
            post(router_confidence=float("nan"))

    def test_outbox_rejects_unaccepted_or_unbound_audit(self):
        with tempfile.TemporaryDirectory() as directory:
            outbox = PendingPostOutbox(Path(directory) / "posts.sqlite3")
            with self.assertRaises(SubmissionError):
                outbox.enqueue(post(), audit_for(post(), drift_score=6.0))
            with self.assertRaises(SubmissionError):
                outbox.enqueue(post(), audit_for(post(), payload={"text": "別の本文"}))
            with self.assertRaises(SubmissionError):
                outbox.enqueue(post(), audit_for(post(), character_id="ena"))
            self.assertEqual(
                outbox.db.execute("SELECT count(*) FROM pending_posts").fetchone()[0],
                0,
            )
            outbox.close()


if __name__ == "__main__":
    unittest.main()
