from fastapi.testclient import TestClient

from main import app, idempotency_records, publication_history, published_posts

client = TestClient(app)


def reset_state():
    idempotency_records.clear()
    publication_history.clear()
    published_posts.clear()


def publish_request(rendition, destination, key):
    return client.post(
        "/connector/v1/discourse/publish",
        json={
            "rendition": rendition,
            "destination": destination,
            "connection": {},
            "idempotency_key": key,
        },
    )


def test_promotional_content_blocked_when_destination_disallows_it():
    reset_state()
    rendition = {
        "title": "Promotion",
        "content": "Check out our product.",
        "is_promotional": True,
    }
    destination = {"surface": "topic", "self_promo_allowed": False}

    response = publish_request(rendition, destination, "promo-block-1")

    assert response.status_code == 422
    assert response.json()["error"]["details"]["violations"][0]["field"] == "self_promotion"


def test_links_blocked_by_destination_link_policy():
    reset_state()
    rendition = {
        "title": "Article with link",
        "content": "Read https://example.com/article",
    }
    destination = {"surface": "topic", "link_policy": "no_links"}

    response = publish_request(rendition, destination, "link-block-1")

    assert response.status_code == 422
    assert any(
        item["field"] == "content"
        for item in response.json()["error"]["details"]["violations"]
    )


def test_ai_generated_content_requires_disclosure_when_configured():
    reset_state()
    rendition = {
        "title": "AI article",
        "content": "An article created with AI.",
        "ai_generated": True,
    }
    destination = {"surface": "topic", "disclosure_required": True}

    response = publish_request(rendition, destination, "ai-disclosure-1")

    assert response.status_code == 422
    assert any(
        item["field"] == "ai_disclosure"
        for item in response.json()["error"]["details"]["violations"]
    )


def test_ai_generated_content_allowed_when_disclosed():
    reset_state()
    rendition = {
        "title": "AI article",
        "content": "AI-generated content: this is a sample article.",
        "ai_generated": True,
    }
    destination = {"surface": "topic", "disclosure_required": True}

    response = publish_request(rendition, destination, "ai-disclosure-2")

    assert response.status_code == 200
    assert response.json()["status"] == "simulated"


def test_frequency_cap_blocks_second_distinct_post():
    reset_state()
    destination = {
        "surface": "topic",
        "url": "https://forum.example.com/c/community",
        "frequency_cap": {"max_posts": 1, "window_days": 7},
    }
    first = publish_request(
        {"title": "First post", "content": "First content."},
        destination,
        "frequency-first-1",
    )
    second = publish_request(
        {"title": "Second post", "content": "Second content."},
        destination,
        "frequency-second-1",
    )

    assert first.status_code == 200
    assert second.status_code == 429
    assert second.json()["error"]["code"] == "DEST_RULE_FREQUENCY_CAP"
    assert second.json()["error"]["retryable"] is True


def test_idempotent_retry_does_not_hit_frequency_cap():
    reset_state()
    destination = {
        "surface": "topic",
        "url": "https://forum.example.com/c/community",
        "frequency_cap": {"max_posts": 1, "window_days": 7},
    }
    rendition = {"title": "First post", "content": "First content."}
    first = publish_request(rendition, destination, "frequency-retry-1")
    retry = publish_request(rendition, destination, "frequency-retry-1")

    assert first.status_code == 200
    assert retry.status_code == 200
    assert retry.json()["platform_post_id"] == first.json()["platform_post_id"]
