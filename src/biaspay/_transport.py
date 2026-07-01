"""HTTP transport: request building, retries, and response handling.

The pure helpers (:func:`build_request`, :func:`prepare_retry`, response
parsing) are shared between the sync and async clients. Only the actual send
differs, so each client owns its ``httpx`` client and calls these helpers.
"""

from __future__ import annotations

import json
import platform
import uuid
from dataclasses import dataclass, field
from importlib.metadata import PackageNotFoundError, version
from typing import Any, List, Mapping, Optional, Sequence, Tuple

import httpx
from pydantic import BaseModel

from ._exceptions import (
    APIConnectionError,
    APITimeoutError,
    exception_from_response,
)

API_VERSION = "2025-07-01"
DEFAULT_BASE_URL = "https://api.biaspay.com"
DEFAULT_TIMEOUT = 30.0
DEFAULT_MAX_RETRIES = 1
MAX_BACKOFF_SECONDS = 15.0


def _sdk_version() -> str:
    try:
        return version("biaspay")
    except PackageNotFoundError:
        return "0.0.0"


def _user_agent() -> str:
    return f"Bias Python SDK/v{_sdk_version()} (Python/{platform.python_version()})"


@dataclass
class PreparedRequest:
    """Everything needed to issue one HTTP request (before retries)."""

    method: str
    url: str
    headers: dict[str, str]
    content: Optional[bytes] = None
    files: Optional[dict[str, Any]] = None


@dataclass
class TransportConfig:
    api_key: Optional[str]
    base_url: str = DEFAULT_BASE_URL
    timeout: float = DEFAULT_TIMEOUT
    max_retries: int = DEFAULT_MAX_RETRIES
    default_headers: Mapping[str, str] = field(default_factory=dict)


def _interpolate_path(path: str, path_params: Mapping[str, Any]) -> str:
    def repl(name: str) -> str:
        if name not in path_params or path_params[name] is None:
            raise ValueError(f"Missing required path parameter: {name}")
        return str(path_params[name])

    out = path
    for name, value in path_params.items():
        out = out.replace("{" + name + "}", repl(name))
    if "{" in out:
        # A placeholder was left unfilled.
        missing = out[out.index("{") + 1 : out.index("}")]
        raise ValueError(f"Missing required path parameter: {missing}")
    return out


def _jsonable_body(value: Any) -> Any:
    """Recursively convert a request body into JSON-serializable values.

    Pydantic models (the generated request/param types) become plain dicts via
    ``model_dump`` — ``by_alias`` so aliased reserved-word fields serialize to
    their wire names, ``exclude_none`` to match the SDK's drop-``None`` behavior.
    Dicts and lists are walked so models nested inside them are converted too.
    This lets callers pass typed models directly (e.g. ``PaymentMethodCardCreate``)
    instead of hand-building dicts.
    """
    if isinstance(value, BaseModel):
        return value.model_dump(by_alias=True, exclude_none=True)
    if isinstance(value, Mapping):
        return {k: _jsonable_body(v) for k, v in value.items() if v is not None}
    if isinstance(value, (list, tuple)):
        return [_jsonable_body(v) for v in value]
    return value


def _encode_query(query: Mapping[str, Any]) -> Sequence[Tuple[str, str]]:
    """Drop ``None`` values; JSON-encode nested objects/arrays (matches the TS SDK)."""
    params: List[Tuple[str, str]] = []
    for key, value in query.items():
        if value is None:
            continue
        if isinstance(value, (dict, list)):
            params.append((key, json.dumps(value, separators=(",", ":"))))
        elif isinstance(value, bool):
            params.append((key, "true" if value else "false"))
        else:
            params.append((key, str(value)))
    return params


def build_request(
    config: TransportConfig,
    method: str,
    path: str,
    *,
    path_params: Optional[Mapping[str, Any]] = None,
    query: Optional[Mapping[str, Any]] = None,
    body: Optional[Mapping[str, Any]] = None,
    content_type: str = "application/json",
    idempotency_key: Optional[str] = None,
    extra_headers: Optional[Mapping[str, str]] = None,
) -> PreparedRequest:
    path_params = path_params or {}
    interpolated = _interpolate_path(path, path_params)
    url = config.base_url.rstrip("/") + interpolated

    qp = _encode_query(query or {})
    if qp:
        url = url + "?" + str(httpx.QueryParams(tuple(qp)))

    headers: dict[str, str] = {
        "user-agent": _user_agent(),
        "api-version": API_VERSION,
        "accept": "application/json",
    }
    if config.api_key is not None:
        headers["authorization"] = f"Bearer {config.api_key}"
    if method in ("POST", "PATCH", "PUT"):
        headers["idempotency-key"] = idempotency_key or str(uuid.uuid4())
    headers.update({k.lower(): v for k, v in config.default_headers.items()})
    if extra_headers:
        headers.update({k.lower(): v for k, v in extra_headers.items()})

    content: Optional[bytes] = None
    files: Optional[dict[str, Any]] = None
    if body:
        if content_type == "multipart/form-data":
            files = {k: v for k, v in body.items() if v is not None}  # httpx sets the boundary
        else:
            headers["content-type"] = "application/json"
            clean = _jsonable_body(body)
            content = json.dumps(clean, separators=(",", ":")).encode("utf-8")

    return PreparedRequest(method=method, url=url, headers=headers, content=content, files=files)


def should_retry(status_code: int) -> bool:
    return status_code == 429 or status_code >= 500


def backoff_seconds(attempt: int, retry_after: Optional[str]) -> float:
    if retry_after:
        try:
            return min(float(retry_after), MAX_BACKOFF_SECONDS)
        except ValueError:
            pass
    return min(2**attempt * 0.01, MAX_BACKOFF_SECONDS)


def parse_response(response: httpx.Response) -> Any:
    """Turn a successful response into JSON, bytes (file), or raise on error.

    Returns the raw parsed value; the resource layer is responsible for casting
    into Pydantic models.
    """
    status = response.status_code
    content_type = response.headers.get("content-type", "")

    if 200 <= status < 300:
        if "application/json" in content_type:
            return response.json()
        # Binary download (e.g. invoice PDF, file view).
        filename = None
        disposition = response.headers.get("content-disposition")
        if disposition and "filename=" in disposition:
            filename = disposition.split("filename=", 1)[1].strip().strip('"')
        return BiasFile(content=response.content, filename=filename, content_type=content_type)

    body: Any
    try:
        body = response.json()
    except (json.JSONDecodeError, ValueError):
        body = {"error": {"message": response.text}}
    raise exception_from_response(status, response.headers, body)


def raise_for_stream_status(response: httpx.Response) -> None:
    """Raise the mapped API error if a streaming response is not 2xx.

    The body must be read before iterating the stream, so callers invoke this
    after ``response.read()`` on a non-success status.
    """
    status = response.status_code
    if 200 <= status < 300:
        return
    body: Any
    try:
        body = response.json()
    except (json.JSONDecodeError, ValueError):
        body = {"error": {"message": response.text}}
    raise exception_from_response(status, response.headers, body)


def parse_stream_line(line: str) -> Optional[Any]:
    """Parse one line of a newline-delimited JSON stream.

    Returns the decoded value, or ``None`` for keep-alive heartbeats (bare
    whitespace the server sends to hold the connection open).
    """
    line = line.strip()
    if not line:
        return None
    return json.loads(line)


@dataclass
class BiasFile:
    """A binary file returned by the API (e.g. a PDF or uploaded file)."""

    content: bytes
    filename: Optional[str] = None
    content_type: Optional[str] = None


def wrap_transport_error(exc: httpx.HTTPError) -> APIConnectionError:
    if isinstance(exc, httpx.TimeoutException):
        return APITimeoutError()
    return APIConnectionError(str(exc) or "Connection error", cause=exc)
