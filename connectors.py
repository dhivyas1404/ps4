"""Connector adapters for the IncuBrix destination network prototype.

The default adapter is deliberately simulated. It never calls a real platform
API and never claims that a post is live.
"""

from copy import deepcopy
from threading import RLock
from uuid import uuid4


class SimulatedConnector:
    """In-memory connector implementing validate/publish/status/delete.

    This adapter is useful for local development and certification tests. Its
    state is lost when the Python process restarts.
    """

    def __init__(self):
        self.results = {}
        self.posts = {}
        self._lock = RLock()

    def validate(self, article):
        """Validate the simplified Article model used by POST /publish."""
        violations = []
        title = getattr(article, "title", "")
        content = getattr(article, "content", "")

        if not isinstance(title, str) or not title.strip():
            violations.append("Title cannot be empty")
        if not isinstance(content, str) or not content.strip():
            violations.append("Content cannot be empty")

        return {"ok": not violations, "violations": violations}

    def publish(self, article, idempotency_key):
        """Simulate publishing an Article, returning the same result on retry."""
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise ValueError("Idempotency key cannot be empty")

        fingerprint = self._article_fingerprint(article)
        with self._lock:
            existing = self.results.get(idempotency_key)
            if existing is not None:
                if existing["fingerprint"] != fingerprint:
                    raise ValueError(
                        "Idempotency key was already used with different content"
                    )
                return deepcopy(existing["result"])

            post_id = str(uuid4())
            result = {
                "platform_post_id": post_id,
                "live_url": None,
                "status": "simulated",
                "destination": getattr(article, "destination", "unknown"),
                "idempotency_key": idempotency_key,
            }
            self.results[idempotency_key] = {
                "fingerprint": fingerprint,
                "result": deepcopy(result),
            }
            self.posts[post_id] = deepcopy(result)
            return deepcopy(result)

    def get_status(self, platform_post_id, connection=None):
        """Return simulated status for a previously created post."""
        del connection  # Reserved for real adapters; no credentials are needed here.
        with self._lock:
            post = self.posts.get(platform_post_id)
            if post is None:
                return None
            return {
                "status": post["status"],
                "live_url": post["live_url"],
            }

    def delete(self, platform_post_id, connection=None):
        """Mark a simulated post as deleted; does not contact a platform."""
        del connection
        with self._lock:
            post = self.posts.get(platform_post_id)
            if post is None:
                return {"deleted": False}
            post["status"] = "deleted"
            post["live_url"] = None
            return {"deleted": True}

    @staticmethod
    def _article_fingerprint(article):
        """Create a stable fingerprint without depending on a specific model."""
        title = getattr(article, "title", "")
        content = getattr(article, "content", "")
        destination = getattr(article, "destination", "")
        return f"{destination}\0{title}\0{content}"
