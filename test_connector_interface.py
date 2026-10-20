from types import SimpleNamespace

from connectors import SimulatedConnector


def make_article(title="Example", content="Hello world", destination="discourse"):
    return SimpleNamespace(title=title, content=content, destination=destination)


def test_validate_rejects_blank_title_and_content():
    connector = SimulatedConnector()
    result = connector.validate(make_article(title="  ", content=""))
    assert result["ok"] is False
    assert "Title cannot be empty" in result["violations"]
    assert "Content cannot be empty" in result["violations"]


def test_publish_retry_returns_same_result():
    connector = SimulatedConnector()
    article = make_article()
    first = connector.publish(article, "request-1")
    second = connector.publish(article, "request-1")
    assert first == second
    assert first["status"] == "simulated"
    assert first["live_url"] is None


def test_same_key_with_different_content_is_rejected():
    connector = SimulatedConnector()
    connector.publish(make_article(content="first"), "request-1")
    try:
        connector.publish(make_article(content="second"), "request-1")
    except ValueError as exc:
        assert "different content" in str(exc)
    else:
        raise AssertionError("Expected different-content retry to be rejected")


def test_status_and_delete_lifecycle():
    connector = SimulatedConnector()
    result = connector.publish(make_article(), "request-1")
    post_id = result["platform_post_id"]

    assert connector.get_status(post_id) == {
        "status": "simulated",
        "live_url": None,
    }
    assert connector.delete(post_id) == {"deleted": True}
    assert connector.get_status(post_id) == {
        "status": "deleted",
        "live_url": None,
    }
    assert connector.delete("missing-id") == {"deleted": False}


def test_empty_idempotency_key_is_rejected():
    connector = SimulatedConnector()
    try:
        connector.publish(make_article(), "  ")
    except ValueError as exc:
        assert "cannot be empty" in str(exc)
    else:
        raise AssertionError("Expected empty idempotency key to be rejected")
