"""Shared OpenAPI error-response shape (Phase 9).

FastAPI's default `HTTPException` renders as `{"detail": <str>}` — this
model documents that exact shape so every route's `responses={...}`
dict can reference one shared schema instead of restating it, and so
`contracts/openapi.v1.json` has a real, named schema for error bodies
rather than leaving every 4xx response undocumented (that was the
state of the world before this phase — see the README's "/v1 API
contract" section).
"""

from typing import Any

from fastapi import status
from pydantic import BaseModel, Field


class ErrorDetail(BaseModel):
    detail: str = Field(examples=["Not found"])


# Matches FastAPI's own `responses` parameter type exactly (dict is
# invariant in its key type, so this must be `int | str`, not just
# `int`, even though every fragment below only ever uses int keys).
_Responses = dict[int | str, dict[str, Any]]


def merge_responses(*fragments: _Responses) -> _Responses:
    """Combines several of the fragments below into one `responses=`
    dict for a route decorator — e.g.
    `responses=merge_responses(UNAUTHORIZED, NOT_FOUND)`. A plain
    dict-literal spread (`{**UNAUTHORIZED, **NOT_FOUND}`) works
    identically at runtime but trips a mypy dict-unpacking inference
    edge case across several call sites; this sidesteps that.
    """
    merged: _Responses = {}
    for fragment in fragments:
        merged.update(fragment)
    return merged


# Reusable `responses={...}` fragments for FastAPI route decorators —
# spread these into a route's `responses=` dict (`{**UNAUTHORIZED,
# **NOT_FOUND}`) rather than restating the same status/model/description
# at every call site. 422 is deliberately never included: FastAPI already
# documents it automatically, with its own more precise
# HTTPValidationError schema, for every route with a request body —
# overriding that with ErrorDetail would be less accurate, not more.
UNAUTHORIZED: _Responses = {
    status.HTTP_401_UNAUTHORIZED: {
        "model": ErrorDetail,
        "description": "Missing, invalid, or expired credentials.",
    }
}
FORBIDDEN: _Responses = {
    status.HTTP_403_FORBIDDEN: {
        "model": ErrorDetail,
        "description": "Authenticated, but not permitted to perform this action.",
    }
}
NOT_FOUND: _Responses = {
    status.HTTP_404_NOT_FOUND: {
        "model": ErrorDetail,
        "description": (
            "Not found. Also returned for a resource that belongs to another "
            "practice — cross-tenant access is never distinguished from "
            "not-existing, so a guess can't confirm a resource exists."
        ),
    }
}
BAD_REQUEST: _Responses = {
    status.HTTP_400_BAD_REQUEST: {"model": ErrorDetail, "description": "Malformed request."}
}
CONFLICT: _Responses = {
    status.HTTP_409_CONFLICT: {
        "model": ErrorDetail,
        "description": "The request conflicts with the resource's current state.",
    }
}
PAYLOAD_TOO_LARGE: _Responses = {
    status.HTTP_413_REQUEST_ENTITY_TOO_LARGE: {
        "model": ErrorDetail,
        "description": "Request payload exceeds the configured size limit.",
    }
}
RATE_LIMITED: _Responses = {
    status.HTTP_429_TOO_MANY_REQUESTS: {
        "model": ErrorDetail,
        "description": "Rate limit exceeded — see the Retry-After header.",
        "headers": {
            "Retry-After": {
                "schema": {"type": "integer"},
                "description": "Seconds to wait before retrying.",
            }
        },
    }
}
UPSTREAM_VENDOR_FAILURE: _Responses = {
    status.HTTP_502_BAD_GATEWAY: {
        "model": ErrorDetail,
        "description": "The upstream transcription vendor call failed.",
    },
    status.HTTP_503_SERVICE_UNAVAILABLE: {
        "model": ErrorDetail,
        "description": "The upstream transcription vendor rate-limited this request.",
    },
    status.HTTP_504_GATEWAY_TIMEOUT: {
        "model": ErrorDetail,
        "description": "The upstream transcription vendor call timed out.",
    },
}
