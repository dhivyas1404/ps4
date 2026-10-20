import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from uuid import uuid4

import yaml
from fastapi import FastAPI, Header, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from connectors import SimulatedConnector


app = FastAPI(
    title="IncuBrix PS4",
    description="Long-Tail Destination Publishing Service",
    version="0.5.0",
)

connector = SimulatedConnector()
state_lock = Lock()

SUPPORTED_CONNECTORS = {
    "discourse", "lemmy", "devto", "ghost",
    "webhook", "assisted",
}

ALLOWED_DESTINATIONS = {
    "discourse", "lemmy", "devto", "ghost",
}

published_posts = {}
idempotency_records = {}
publication_history = {}


# -------------------------
# Load connector manifests
# -------------------------

BASE_DIR = Path(__file__).resolve().parent
MANIFEST_DIR = BASE_DIR / "manifests"


def load_manifests():
    manifests = {}

    if not MANIFEST_DIR.exists():
        return manifests

    for file_path in MANIFEST_DIR.glob("*.yaml"):
        with file_path.open("r", encoding="utf-8") as file:
            manifest = yaml.safe_load(file) or {}

        key = manifest.get("key")

        if not key:
            raise ValueError(
                f"Manifest has no connector key: {file_path.name}"
            )

        if key in manifests:
            raise ValueError(
                f"Duplicate manifest key: {key}"
            )

        manifests[key] = manifest

    return manifests


CONNECTOR_MANIFESTS = load_manifests()


def get_manifest_spec(connector_key, destination):
    """Read default format rules from a connector manifest."""
    manifest = CONNECTOR_MANIFESTS.get(connector_key, {})
    surfaces = manifest.get("surfaces", [])

    requested_surface = destination.get("surface")

    selected_surface = next(
        (
            surface for surface in surfaces
            if requested_surface
            and surface.get("key") == requested_surface
        ),
        surfaces[0] if surfaces else {},
    )

    return selected_surface.get("spec", {})


# -------------------------
# Request models
# -------------------------

class Article(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    content: str = Field(min_length=1)
    destination: str = Field(min_length=1)


class ConnectorValidationRequest(BaseModel):
    rendition: dict
    destination: dict
    connection: dict = Field(default_factory=dict)


class ConnectorPublishRequest(BaseModel):
    rendition: dict
    destination: dict
    connection: dict = Field(default_factory=dict)
    idempotency_key: str = Field(min_length=1)


class ConnectorStatusRequest(BaseModel):
    platform_post_id: str = Field(min_length=1)
    connection: dict = Field(default_factory=dict)


class ConnectorDeleteRequest(BaseModel):
    platform_post_id: str = Field(min_length=1)
    connection: dict = Field(default_factory=dict)


# -------------------------
# Standard error responses
# -------------------------

def error_response(
    code,
    message,
    retryable=False,
    details=None,
):
    return {
        "error": {
            "code": code,
            "message": message,
            "retryable": retryable,
            "details": details or {},
        }
    }


@app.exception_handler(HTTPException)
async def http_error_handler(request, exc):
    code_by_status = {
        400: "DESTINATION_NOT_SUPPORTED",
        404: "NOT_FOUND",
        409: "IDEMPOTENCY_KEY_CONFLICT",
        422: "DEST_SPEC_VIOLATION",
    }

    code = code_by_status.get(exc.status_code, "REQUEST_ERROR")
    message = exc.detail
    details = {}
    retryable = False

    if isinstance(exc.detail, dict):
        code = exc.detail.get("code", code)
        message = exc.detail.get("message", "Request failed")
        details = exc.detail.get("details", {})
        retryable = exc.detail.get("retryable", False)
        # Preserve validation detail fields for existing clients.
        if "violations" in exc.detail:
            details = {**details, "violations": exc.detail["violations"]}

    return JSONResponse(
        status_code=exc.status_code,
        content=error_response(
            code=code,
            message=message,
            retryable=retryable,
            details=details,
        ),
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request, exc):
    return JSONResponse(
        status_code=422,
        content=error_response(
            code="DEST_SPEC_VIOLATION",
            message="The request does not match the required format",
            details={"validation_errors": exc.errors()},
        ),
    )


# -------------------------
# Connector and validation helpers
# -------------------------

def check_connector(connector_key):
    if connector_key not in SUPPORTED_CONNECTORS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported connector: {connector_key}",
        )


def validate_rendition(
    rendition,
    destination=None,
    connector_key=None,
):
    destination = destination or {}

    # Start with rules from the connector manifest.
    spec = dict(get_manifest_spec(
        connector_key,
        destination,
    ))

    # Explicit request rules override manifest defaults.
    spec.update(destination.get("spec", {}))

    title_max = spec.get("title_max_chars", 255)
    body_max = spec.get("body_max_chars", 32000)
    links_allowed = spec.get("links_allowed", True)

    violations = []

    title = rendition.get("title", "")
    content = rendition.get(
        "content",
        rendition.get("body", ""),
    )

    if not isinstance(title, str) or not title.strip():
        violations.append({
            "field": "title",
            "reason": "Title is required",
        })
    elif len(title) > title_max:
        violations.append({
            "field": "title",
            "reason": (
                f"Title exceeds the maximum of "
                f"{title_max} characters"
            ),
        })

    if not isinstance(content, str) or not content.strip():
        violations.append({
            "field": "content",
            "reason": "Content is required",
        })
    elif len(content) > body_max:
        violations.append({
            "field": "content",
            "reason": (
                f"Content exceeds the maximum of "
                f"{body_max} characters"
            ),
        })

    if (
        isinstance(content, str)
        and not links_allowed
        and re.search(r"https?://\S+", content)
    ):
        violations.append({
            "field": "content",
            "reason": "Links are not allowed for this destination",
        })

    return violations


def _content_is_promotional(rendition):
    """Only treat content as promotional when the caller explicitly labels it."""
    return rendition.get("is_promotional", rendition.get("promotional", False)) is True


def _content_is_ai_generated(rendition):
    return rendition.get("ai_generated", rendition.get("is_ai_generated", False)) is True


def _has_ai_disclosure(rendition, content):
    explicit_disclosure = rendition.get("ai_disclosure") or rendition.get("disclosure")
    if isinstance(explicit_disclosure, str) and explicit_disclosure.strip():
        return True
    return bool(re.search(r"\bAI[- ]generated\b", content, flags=re.IGNORECASE))


def _destination_history_key(connector_key, destination):
    """Identify one destination instance, not just its connector type."""
    instance = (
        destination.get("url")
        or destination.get("base_url")
        or destination.get("destination_id")
        or destination.get("name")
        or destination.get("surface")
        or "default"
    )
    return f"{connector_key}:{str(instance).strip().rstrip('/').lower()}"


def _frequency_cap_settings(destination):
    """Read a frequency cap as {max_posts, window_days/window_hours}.

    An integer is supported as a convenience and means that many posts per 7 days.
    """
    cap = destination.get("frequency_cap")
    if cap is None or cap is False:
        return None
    if isinstance(cap, int) and not isinstance(cap, bool):
        settings = {"max_posts": cap, "window_days": 7}
    elif isinstance(cap, dict):
        settings = cap
    else:
        return None

    try:
        max_posts = int(settings.get("max_posts", settings.get("max", 0)))
        if "window_hours" in settings:
            window = timedelta(hours=float(settings["window_hours"]))
        else:
            window = timedelta(days=float(settings.get("window_days", 7)))
    except (TypeError, ValueError):
        return None

    if max_posts < 1 or window.total_seconds() <= 0:
        return None
    return {"max_posts": max_posts, "window": window}


def validate_content_policies(rendition, destination):
    """Validate self-promotion, link policy and AI disclosure requirements."""
    destination = destination or {}
    violations = []
    content = rendition.get("content", rendition.get("body", ""))
    content = content if isinstance(content, str) else ""

    if destination.get("self_promo_allowed", True) is False and _content_is_promotional(rendition):
        violations.append({
            "field": "self_promotion",
            "reason": "This destination does not allow self-promotional content",
        })

    link_policy = str(destination.get("link_policy", "allowed")).strip().lower()
    contains_link = bool(re.search(r"https?://\S+", content))
    if link_policy in {"none", "no_links", "disallow", "forbidden"} and contains_link:
        violations.append({
            "field": "content",
            "reason": "Links are not allowed by this destination's link policy",
        })

    if destination.get("disclosure_required") is True and _content_is_ai_generated(rendition):
        if not _has_ai_disclosure(rendition, content):
            violations.append({
                "field": "ai_disclosure",
                "reason": "This destination requires disclosure for AI-generated content",
            })

    return violations


def check_frequency_cap(connector_key, destination):
    """Return a policy violation when the destination's posting cap is reached."""
    settings = _frequency_cap_settings(destination or {})
    if settings is None:
        return None

    now = datetime.now(timezone.utc)
    history_key = _destination_history_key(connector_key, destination or {})
    with state_lock:
        recent_posts = [
            timestamp for timestamp in publication_history.get(history_key, [])
            if now - timestamp < settings["window"]
        ]
        publication_history[history_key] = recent_posts

    if len(recent_posts) >= settings["max_posts"]:
        oldest = min(recent_posts)
        retry_after = max(1, int((oldest + settings["window"] - now).total_seconds()))
        return {
            "field": "frequency_cap",
            "reason": "Destination posting frequency cap has been reached",
            "retry_after_seconds": retry_after,
            "max_posts": settings["max_posts"],
        }
    return None


def record_publication(connector_key, destination):
    settings = _frequency_cap_settings(destination or {})
    if settings is None:
        return
    history_key = _destination_history_key(connector_key, destination or {})
    with state_lock:
        publication_history.setdefault(history_key, []).append(datetime.now(timezone.utc))


def create_request_fingerprint(
    connector_key,
    rendition,
    destination,
    connection,
):
    request_data = {
        "connector_key": connector_key,
        "rendition": rendition,
        "destination": destination,
        "connection": connection,
    }

    canonical_json = json.dumps(
        request_data,
        sort_keys=True,
        separators=(",", ":"),
    )

    return hashlib.sha256(
        canonical_json.encode("utf-8")
    ).hexdigest()


# -------------------------
# Health endpoints
# -------------------------

@app.get("/")
def home():
    return {
        "message": "IncuBrix PS4 service is running!",
        "status": "success",
    }


@app.get("/health")
def health():
    return {"status": "healthy"}


@app.get("/ready")
def ready():
    return {
        "status": "ready",
        "loaded_connectors": sorted(CONNECTOR_MANIFESTS.keys()),
    }


# -------------------------
# Connector: validate
# -------------------------

@app.post("/connector/v1/{connector_key}/validate")
def validate_connector(
    connector_key: str,
    request: ConnectorValidationRequest,
):
    check_connector(connector_key)

    violations = validate_rendition(
        request.rendition,
        request.destination,
        connector_key,
    )
    violations.extend(validate_content_policies(request.rendition, request.destination))

    return {
        "ok": len(violations) == 0,
        "violations": violations,
    }


# -------------------------
# Connector: publish
# -------------------------

@app.post("/connector/v1/{connector_key}/publish")
def connector_publish(
    connector_key: str,
    request: ConnectorPublishRequest,
):
    check_connector(connector_key)

    violations = validate_rendition(
        request.rendition,
        request.destination,
        connector_key,
    )

    if violations:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "Article validation failed",
                "violations": violations,
            },
        )

    if not request.idempotency_key.strip():
        raise HTTPException(
            status_code=422,
            detail="Idempotency key cannot be empty",
        )

    key = f"{connector_key}:{request.idempotency_key}"

    fingerprint = create_request_fingerprint(
        connector_key,
        request.rendition,
        request.destination,
        request.connection,
    )

    # Resolve retries before applying frequency caps: a retry of a request
    # already accepted must return the original result, not count as a new post.
    with state_lock:
        existing = idempotency_records.get(key)
        if existing is not None:
            if existing["fingerprint"] != fingerprint:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "This idempotency key was already "
                        "used with a different request"
                    ),
                )
            return existing["result"]

    policy_violations = validate_content_policies(
        request.rendition,
        request.destination,
    )
    if policy_violations:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "DEST_SPEC_VIOLATION",
                "message": "Destination posting rules were not satisfied",
                "retryable": False,
                "details": {"violations": policy_violations},
            },
        )

    cap_violation = check_frequency_cap(connector_key, request.destination)
    if cap_violation:
        raise HTTPException(
            status_code=429,
            detail={
                "code": "DEST_RULE_FREQUENCY_CAP",
                "message": "Destination posting frequency cap has been reached",
                "retryable": True,
                "details": cap_violation,
            },
        )

    result = {
        "platform_post_id": str(uuid4()),
        "live_url": None,
        "status": "simulated",
    }

    with state_lock:
        # Check again under the lock in case concurrent requests used the same key.
        existing = idempotency_records.get(key)
        if existing is not None:
            if existing["fingerprint"] != fingerprint:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "This idempotency key was already "
                        "used with a different request"
                    ),
                )
            return existing["result"]

        idempotency_records[key] = {
            "fingerprint": fingerprint,
            "result": result,
        }
        published_posts[result["platform_post_id"]] = {
            "result": result,
            "deleted": False,
        }
        if _frequency_cap_settings(request.destination) is not None:
            history_key = _destination_history_key(connector_key, request.destination)
            publication_history.setdefault(history_key, []).append(datetime.now(timezone.utc))

    return result


# -------------------------
# Connector: get_status
# -------------------------

@app.post("/connector/v1/{connector_key}/get_status")
def connector_get_status(
    connector_key: str,
    request: ConnectorStatusRequest,
):
    check_connector(connector_key)

    with state_lock:
        post = published_posts.get(request.platform_post_id)

        if post is None:
            raise HTTPException(
                status_code=404,
                detail="Published post not found",
            )

        if post["deleted"]:
            return {
                "status": "deleted",
                "live_url": None,
            }

        return {
            "status": post["result"]["status"],
            "live_url": post["result"]["live_url"],
        }


# -------------------------
# Connector: delete
# -------------------------

@app.post("/connector/v1/{connector_key}/delete")
def connector_delete(
    connector_key: str,
    request: ConnectorDeleteRequest,
):
    check_connector(connector_key)

    with state_lock:
        post = published_posts.get(request.platform_post_id)

        if post is None:
            raise HTTPException(
                status_code=404,
                detail="Published post not found",
            )

        post["deleted"] = True
        post["result"]["status"] = "deleted"

        return {"deleted": True}


# -------------------------
# Existing simplified publish endpoint
# -------------------------

@app.post("/publish")
def publish_article(
    article: Article,
    idempotency_key: str = Header(
        default="demo-request",
        alias="Idempotency-Key",
    ),
):
    if article.destination not in ALLOWED_DESTINATIONS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unsupported destination: "
                f"{article.destination}"
            ),
        )

    validation = connector.validate(article)

    if not validation["ok"]:
        raise HTTPException(
            status_code=422,
            detail="Article validation failed",
        )

    if not idempotency_key.strip():
        raise HTTPException(
            status_code=400,
            detail="Idempotency-Key cannot be empty",
        )

    return connector.publish(article, idempotency_key)
