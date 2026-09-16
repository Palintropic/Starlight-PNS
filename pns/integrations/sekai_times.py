"""Sekai Times pending-post transport. No runtime action or automatic publisher lives here."""

import base64
import hashlib
import json
import math
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from pns.models.authored import GenerationAudit

CHARACTERS = {
    "kanade": ("kanade", "宵崎奏"),
    "mafuyu": ("mafuyu", "朝比奈まふゆ"),
    "ena": ("ena", "東雲絵名"),
    "mizuki": ("mzk", "暁山瑞希"),
}


class SubmissionError(ValueError):
    """The proposed post does not meet the outbound contract."""


class DeliveryError(RuntimeError):
    """A safe, redacted WordPress read or contract error."""


class DeliveryRejected(DeliveryError):
    """WordPress returned a client error to a write; reconciliation is still required."""


class DeliveryUncertain(RuntimeError):
    """The write may have landed. A retry must reconcile before another POST."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


@dataclass(frozen=True)
class PendingPost:
    world_id: str
    source_id: str
    session_id: str
    character_id: str
    title: str
    content: str
    drift_score: float
    router_confidence: float

    def __post_init__(self):
        for name in ("world_id", "source_id", "session_id", "title", "content"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise SubmissionError(f"{name} 必须是非空字符串")
        if self.character_id not in CHARACTERS:
            raise SubmissionError("角色尚未映射到 Sekai Times 分类")
        for name, maximum in (("drift_score", 10), ("router_confidence", 1)):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise SubmissionError(f"{name} 必须是数字")
            if not math.isfinite(value) or not 0 <= value <= maximum:
                raise SubmissionError(f"{name} 超出范围")

    @property
    def slug(self):
        digest = hashlib.sha256(
            f"{self.world_id}\0{self.source_id}".encode("utf-8")
        ).hexdigest()[:32]
        return f"pns-{digest}"

    def payload(self, category_id):
        _, character_jp = CHARACTERS[self.character_id]
        return {
            "title": self.title,
            "content": self.content,
            "slug": self.slug,
            "status": "pending",
            "categories": [category_id],
            "meta": {
                "character_jp": character_jp,
                "drift_score": self.drift_score,
                "session_id": self.session_id,
                "pns_submission_id": self.slug,
                "router_confidence": self.router_confidence,
                "interaction_type": "blog_post",
            },
        }


class WordPressPendingPosts:
    @classmethod
    def from_env(cls):
        return cls(
            os.environ.get("PNS_SEKAI_TIMES_URL", ""),
            os.environ.get("PNS_SEKAI_TIMES_USERNAME", ""),
            os.environ.get("PNS_SEKAI_TIMES_APPLICATION_PASSWORD", ""),
        )

    def __init__(self, base_url, username, application_password, *, opener=None):
        parsed = urlparse(base_url)
        local_http = parsed.scheme == "http" and parsed.hostname in {
            "localhost",
            "127.0.0.1",
            "::1",
        }
        if parsed.scheme != "https" and not local_http:
            raise SubmissionError("WordPress 地址必须使用 HTTPS（本地回环测试除外）")
        if (
            not parsed.netloc
            or parsed.username
            or parsed.password
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
        ):
            raise SubmissionError("WordPress 地址必须是站点根地址")
        if not username or not application_password:
            raise SubmissionError("缺少 WordPress Application Password 配置")
        self.base_url = base_url.rstrip("/")
        self.opener = opener if opener is not None else build_opener(_NoRedirect()).open
        token = base64.b64encode(
            f"{username}:{application_password}".encode("utf-8")
        ).decode("ascii")
        self._authorization = f"Basic {token}"

    def _request(self, method, path, body=None, *, expected=None):
        url = self.base_url + "/wp-json/" + path
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body else None
        request = Request(
            url,
            data=data,
            method=method,
            headers={
                "Authorization": self._authorization,
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        try:
            with self.opener(request, timeout=10) as response:
                result = json.load(response)
                status = response.status
        except HTTPError as exc:
            exc.close()
            if method == "POST" and (300 <= exc.code < 400 or exc.code >= 500):
                raise DeliveryUncertain("WordPress 写入结果不确定") from None
            if method == "POST":
                raise DeliveryRejected(f"WordPress 返回 HTTP {exc.code}") from None
            raise DeliveryError(f"WordPress 返回 HTTP {exc.code}") from None
        except (URLError, TimeoutError, OSError, ValueError):
            if method == "POST":
                raise DeliveryUncertain("WordPress 写入结果不确定") from None
            raise DeliveryError("无法读取 WordPress 对账数据") from None
        if not 200 <= status < 300:
            raise DeliveryError(f"WordPress 返回 HTTP {status}")
        if expected is not None and not isinstance(result, expected):
            raise DeliveryError("WordPress 对账响应格式不合法")
        if method == "POST" and not isinstance(result, dict):
            raise DeliveryUncertain("WordPress 创建结果需要人工对账")
        return result

    def verify_contract(self):
        if getattr(self, "_contract_verified", False):
            return
        index = self._request("GET", "", expected=dict)
        route = (index.get("routes") or {}).get("/wp/v2/posts") or {}
        endpoints = route.get("endpoints") or []
        writers = [item for item in endpoints if "POST" in item.get("methods", [])]
        if len(writers) != 1:
            raise DeliveryError("Sekai Times 没有唯一的文章写入契约")
        meta = (writers[0].get("args") or {}).get("meta") or {}
        properties = meta.get("properties") or {}
        if "pns_submission_id" not in properties:
            raise DeliveryError("Sekai Times 尚未部署 pns_submission_id，拒绝写入")
        self._contract_verified = True

    def _category(self, post):
        slug, expected_name = CHARACTERS[post.character_id]
        categories = self._request(
            "GET", "wp/v2/categories?" + urlencode({"slug": slug}), expected=list
        )
        exact = [c for c in categories if c.get("slug") == slug]
        if len(exact) != 1 or exact[0].get("name") != expected_name:
            raise DeliveryError("Sekai Times 角色分类与本地契约不一致")
        term_id = exact[0].get("id")
        if isinstance(term_id, bool) or not isinstance(term_id, int) or term_id <= 0:
            raise DeliveryError("Sekai Times 角色分类 ID 不合法")
        return term_id

    def reconcile(self, post):
        self.verify_contract()
        found = []
        for status in ("pending", "publish", "draft"):
            offset = 0
            while True:
                query = urlencode(
                    {
                        "context": "edit",
                        "status": status,
                        "per_page": 100,
                        "offset": offset,
                        "orderby": "id",
                        "order": "asc",
                    }
                )
                page = self._request("GET", "wp/v2/posts?" + query, expected=list)
                found.extend(page)
                if len(page) < 100:
                    break
                offset += 100
        exact = [
            item
            for item in found
            if (item.get("meta") or {}).get("pns_submission_id") == post.slug
        ]
        if not exact:
            return None
        if len(exact) != 1:
            raise DeliveryError("Sekai Times 投稿来源标记重复")
        item = exact[0]
        meta = item.get("meta") or {}
        if (
            meta.get("session_id") != post.session_id
            or meta.get("pns_submission_id") != post.slug
            or meta.get("character_jp") != CHARACTERS[post.character_id][1]
            or meta.get("interaction_type") != "blog_post"
        ):
            raise DeliveryError("Sekai Times 同名投稿来源不匹配")
        post_id = item.get("id")
        if isinstance(post_id, bool) or not isinstance(post_id, int) or post_id <= 0:
            raise DeliveryError("Sekai Times 投稿 ID 不合法")
        return post_id

    def submit_once(self, post):
        existing = self.reconcile(post)
        if existing is not None:
            return existing
        category_id = self._category(post)
        return self.create_pending(post, category_id)

    def create_pending(self, post, category_id):
        """Call only after reconciliation and category validation."""
        created = self._request(
            "POST", "wp/v2/posts", post.payload(category_id), expected=dict
        )
        meta = created.get("meta") or {}
        if (
            created.get("status") != "pending"
            or meta.get("pns_submission_id") != post.slug
        ):
            raise DeliveryUncertain("WordPress 创建结果需要人工对账")
        post_id = created.get("id")
        if isinstance(post_id, bool) or not isinstance(post_id, int) or post_id <= 0:
            raise DeliveryUncertain("WordPress 创建结果需要人工对账")
        return post_id


class PendingPostOutbox:
    """Durable, one-attempt outbox. Unknown writes are reconciled, never blindly resent."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS pending_posts ("
            "slug TEXT PRIMARY KEY, envelope TEXT NOT NULL, state TEXT NOT NULL, "
            "wp_post_id INTEGER, resolution TEXT)"
        )
        columns = {
            row[1] for row in self.db.execute("PRAGMA table_info(pending_posts)")
        }
        if "resolution" not in columns:
            self.db.execute("ALTER TABLE pending_posts ADD COLUMN resolution TEXT")
        self.db.commit()

    def close(self):
        self.db.close()

    def enqueue(self, post, audit: GenerationAudit):
        self._require_audit(post, audit)
        envelope = json.dumps(
            {"post": post.__dict__, "audit": audit.to_dict()},
            ensure_ascii=False,
            sort_keys=True,
        )
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO pending_posts(slug,envelope,state) VALUES(?,?,'queued')",
                (post.slug, envelope),
            )
            row = self.db.execute(
                "SELECT envelope FROM pending_posts WHERE slug=?", (post.slug,)
            ).fetchone()
            if row[0] != envelope:
                raise SubmissionError("同一投稿来源不能替换正文或元数据")

    @staticmethod
    def _require_audit(post, audit):
        if not isinstance(audit, GenerationAudit) or not audit.accepted:
            raise SubmissionError("投稿缺少已接受的 Router 审核凭据")
        if (
            audit.proposal_id != post.source_id
            or audit.character_id != post.character_id
            or dict(audit.payload) != {"text": post.content, "title": post.title}
            or audit.drift_score != post.drift_score
            or audit.confidence != post.router_confidence
        ):
            raise SubmissionError("Router 审核凭据与投稿来源或正文不匹配")

    def dispatch(self, slug, client):
        row = self.db.execute(
            "SELECT envelope,state,wp_post_id FROM pending_posts WHERE slug=?", (slug,)
        ).fetchone()
        if row is None:
            raise SubmissionError("出站队列里没有这份投稿")
        envelope, state, post_id = row
        if state == "sent":
            return post_id
        if state == "blocked":
            raise DeliveryError("这份投稿已经被 WordPress 明确拒绝")
        decoded = json.loads(envelope)
        post = PendingPost(**decoded["post"])
        audit = GenerationAudit.from_dict(decoded["audit"])
        self._require_audit(post, audit)
        if state == "uncertain":
            found = client.reconcile(post)
            if found is None:
                raise DeliveryUncertain("尚未找到可能已提交的文章；不会自动重发")
        else:
            # Read failures happen before the write boundary and leave this item queued.
            found = client.reconcile(post)
            if found is not None:
                with self.db:
                    self.db.execute(
                        "UPDATE pending_posts SET state='sent',wp_post_id=? WHERE slug=?",
                        (found, slug),
                    )
                return found
            category_id = client._category(post)
            # Claim atomically before the network write: a second process cannot POST the same source.
            with self.db:
                claimed = self.db.execute(
                    "UPDATE pending_posts SET state='uncertain' WHERE slug=? AND state='queued'",
                    (slug,),
                ).rowcount
            if claimed != 1:
                raise DeliveryUncertain("另一投递者已领取这份投稿；请先对账")
            try:
                found = client.create_pending(post, category_id)
            except DeliveryRejected:
                found = client.reconcile(post)
                if found is None:
                    with self.db:
                        self.db.execute(
                            "UPDATE pending_posts SET state='blocked' WHERE slug=?",
                            (slug,),
                        )
                    raise
        with self.db:
            self.db.execute(
                "UPDATE pending_posts SET state='sent',wp_post_id=? WHERE slug=?",
                (found, slug),
            )
        return found

    def release_for_retry(self, slug, client, *, resolution):
        """Requeue an unresolved write only after an operator records why it is safe."""
        if not isinstance(resolution, str) or not resolution.strip():
            raise SubmissionError("人工重试必须记录非空 resolution")
        row = self.db.execute(
            "SELECT envelope,state FROM pending_posts WHERE slug=?", (slug,)
        ).fetchone()
        if row is None:
            raise SubmissionError("出站队列里没有这份投稿")
        envelope, state = row
        if state not in ("blocked", "uncertain"):
            raise SubmissionError("只有 blocked 或 uncertain 投稿能人工解除")
        decoded = json.loads(envelope)
        post = PendingPost(**decoded["post"])
        found = client.reconcile(post)
        with self.db:
            if found is not None:
                self.db.execute(
                    "UPDATE pending_posts SET state='sent',wp_post_id=?,resolution=? "
                    "WHERE slug=?",
                    (found, resolution.strip(), slug),
                )
                return found
            self.db.execute(
                "UPDATE pending_posts SET state='queued',resolution=? WHERE slug=?",
                (resolution.strip(), slug),
            )
        return None
