
from fastapi.testclient import TestClient

from main import app, CONNECTOR_MANIFESTS


client = TestClient(app)


def test_health():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "healthy"}


def test_ready():
    response = client.get("/ready")
    assert response.status_code == 200
    assert response.json()["status"] == "ready"


def test_validate_valid_article():
    response = client.post(
        "/connector/v1/discourse/validate",
        json={
            "rendition": {
                "title": "Test Article",
                "content": "This is valid test content.",
            },
            "destination": {"name": "discourse"},
            "connection": {},
        },
    )

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.json()["violations"] == []


def test_validate_missing_title():
    response = client.post(
        "/connector/v1/discourse/validate",
        json={
            "rendition": {
                "title": "",
                "content": "This is test content.",
            },
            "destination": {"name": "discourse"},
            "connection": {},
        },
    )

    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert len(response.json()["violations"]) > 0


def test_unknown_connector():
    response = client.post(
        "/connector/v1/unknown/validate",
        json={
            "rendition": {
                "title": "Test",
                "content": "Test content",
            },
            "destination": {},
            "connection": {},
        },
    )

    assert response.status_code == 400


def test_connector_publish_idempotency():
    payload = {
        "rendition": {
            "title": "Idempotency Test",
            "content": "Testing repeated requests.",
        },
        "destination": {"name": "discourse"},
        "connection": {},
        "idempotency_key": "retry-test-001",
    }

    first = client.post(
        "/connector/v1/discourse/publish",
        json=payload,
    )
    second = client.post(
        "/connector/v1/discourse/publish",
        json=payload,
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert (
        first.json()["platform_post_id"]
        == second.json()["platform_post_id"]
    )


def test_different_idempotency_keys_create_different_posts():
    base_payload = {
        "rendition": {
            "title": "Different Key Test",
            "content": "Testing distinct requests.",
        },
        "destination": {"name": "discourse"},
        "connection": {},
    }

    first = client.post(
        "/connector/v1/discourse/publish",
        json={
            **base_payload,
            "idempotency_key": "different-key-001",
        },
    )
    second = client.post(
        "/connector/v1/discourse/publish",
        json={
            **base_payload,
            "idempotency_key": "different-key-002",
        },
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert (
        first.json()["platform_post_id"]
        != second.json()["platform_post_id"]
    )


def test_deleted_post_status():
    payload = {
        "rendition": {
            "title": "Delete Status Test",
            "content": "Testing deletion status.",
        },
        "destination": {"name": "discourse"},
        "connection": {},
        "idempotency_key": "delete-status-test-001",
    }

    publish_response = client.post(
        "/connector/v1/discourse/publish",
        json=payload,
    )
    assert publish_response.status_code == 200

    post_id = publish_response.json()["platform_post_id"]

    delete_response = client.post(
        "/connector/v1/discourse/delete",
        json={
            "platform_post_id": post_id,
            "connection": {},
        },
    )
    assert delete_response.status_code == 200
    assert delete_response.json() == {"deleted": True}

    status_response = client.post(
        "/connector/v1/discourse/get_status",
        json={
            "platform_post_id": post_id,
            "connection": {},
        },
    )

    assert status_response.status_code == 200
    assert status_response.json()["status"] == "deleted"


def test_same_key_with_different_content_returns_conflict():
    first_payload = {
        "rendition": {
            "title": "Original Article",
            "content": "Original content.",
        },
        "destination": {"name": "discourse"},
        "connection": {},
        "idempotency_key": "conflict-test-001",
    }

    second_payload = {
        **first_payload,
        "rendition": {
            "title": "Changed Article",
            "content": "Different content.",
        },
    }

    first = client.post(
        "/connector/v1/discourse/publish",
        json=first_payload,
    )
    second = client.post(
        "/connector/v1/discourse/publish",
        json=second_payload,
    )

    assert first.status_code == 200
    assert second.status_code == 409
    assert (
        second.json()["error"]["code"]
        == "IDEMPOTENCY_KEY_CONFLICT"
    )


def test_title_limit_from_destination_spec():
    response = client.post(
        "/connector/v1/discourse/validate",
        json={
            "rendition": {
                "title": "This title is too long",
                "content": "Valid content.",
            },
            "destination": {
                "name": "discourse",
                "spec": {
                    "title_max_chars": 10,
                    "body_max_chars": 100,
                    "links_allowed": True,
                },
            },
            "connection": {},
        },
    )

    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert any(
        v["field"] == "title"
        for v in response.json()["violations"]
    )


def test_body_limit_from_destination_spec():
    response = client.post(
        "/connector/v1/discourse/validate",
        json={
            "rendition": {
                "title": "Valid title",
                "content": "A" * 21,
            },
            "destination": {
                "name": "discourse",
                "spec": {
                    "title_max_chars": 100,
                    "body_max_chars": 20,
                    "links_allowed": True,
                },
            },
            "connection": {},
        },
    )

    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert any(
        v["field"] == "content"
        for v in response.json()["violations"]
    )


def test_links_rejected_when_destination_disallows_them():
    response = client.post(
        "/connector/v1/discourse/validate",
        json={
            "rendition": {
                "title": "Valid title",
                "content": "Read more at https://example.com/article",
            },
            "destination": {
                "name": "discourse",
                "spec": {
                    "title_max_chars": 100,
                    "body_max_chars": 1000,
                    "links_allowed": False,
                },
            },
            "connection": {},
        },
    )

    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert any(
        "Links are not allowed" in v["reason"]
        for v in response.json()["violations"]
    )


def test_discourse_manifest_loaded():
    assert "discourse" in CONNECTOR_MANIFESTS

    manifest = CONNECTOR_MANIFESTS["discourse"]

    assert manifest["version"] == "1.0.0"
    assert manifest["surfaces"][0]["key"] == "topic"


def test_manifest_title_limit_is_used():
    response = client.post(
        "/connector/v1/discourse/validate",
        json={
            "rendition": {
                "title": "T" * 256,
                "content": "Valid content.",
            },
            "destination": {"name": "discourse"},
            "connection": {},
        },
    )

    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert any(
        v["field"] == "title"
        for v in response.json()["violations"]
    )
