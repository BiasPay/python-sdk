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
from ._types import _EXPLICIT_NULL, NOT_GIVEN

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
    data: Optional[dict[str, str]] = None
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


def _model_exclusions(value: BaseModel) -> dict[Any, Any]:
    """Build Pydantic exclusions for omitted sentinels and legacy ``None`` values."""
    exclusions: dict[Any, Any] = {}
    for name, model_field in type(value).model_fields.items():
        item = getattr(value, name)
        if item is NOT_GIVEN or (item is None and model_field.default is not NOT_GIVEN):
            exclusions[name] = True
            continue
        nested = _nested_model_exclusions(item)
        if nested:
            exclusions[name] = nested
    return exclusions


def _nested_model_exclusions(value: Any) -> Optional[dict[Any, Any]]:
    if isinstance(value, BaseModel):
        return _model_exclusions(value)
    if isinstance(value, Mapping):
        nested = {
            key: child for key, item in value.items() if (child := _nested_model_exclusions(item))
        }
        return nested or None
    if isinstance(value, (list, tuple)):
        nested = {
            index: child
            for index, item in enumerate(value)
            if (child := _nested_model_exclusions(item))
        }
        return nested or None
    return None


def _jsonable_body(body: Mapping[str, Any]) -> dict[str, Any]:
    """Convert a request body into JSON-serializable values.

    Generated nullable request-model fields default to ``NOT_GIVEN``. Fields set
    explicitly to ``None`` are retained as JSON null, while omitted sentinel
    fields and ordinary ``None`` method arguments are dropped. Nested dicts are
    TypedDict params, where an omitted key means omitted, so a ``None`` value
    there is JSON null.
    """
    return {k: _jsonable(v) for k, v in body.items() if v is not None and v is not NOT_GIVEN}


def _jsonable(value: Any) -> Any:
    if value is _EXPLICIT_NULL:
        return None
    if isinstance(value, BaseModel):
        return value.model_dump(by_alias=True, exclude=_model_exclusions(value))
    if isinstance(value, Mapping):
        return {k: _jsonable(v) for k, v in value.items() if v is not NOT_GIVEN}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value if v is not NOT_GIVEN]
    return value


def _is_file(value: Any) -> bool:
    return isinstance(value, (bytes, bytearray, tuple)) or hasattr(value, "read")


def _form_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _encode_query(query: Mapping[str, Any]) -> Sequence[Tuple[str, str]]:
    """Drop ``None`` values; JSON-encode nested objects/arrays (matches the TS SDK)."""
    params: List[Tuple[str, str]] = []
    for key, value in query.items():
        if value is None or value is NOT_GIVEN or value is _EXPLICIT_NULL:
            continue
        if isinstance(value, (BaseModel, Mapping, list, tuple)):
            params.append((key, json.dumps(_jsonable(value), separators=(",", ":"))))
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
    data: Optional[dict[str, str]] = None
    files: Optional[dict[str, Any]] = None
    if body:
        if content_type == "multipart/form-data":
            # httpx sets the boundary. Only binary values become file parts; the
            # rest are plain form fields.
            fields = {
                k: v
                for k, v in body.items()
                if v is not None and v is not NOT_GIVEN and v is not _EXPLICIT_NULL
            }
            files = {k: v for k, v in fields.items() if _is_file(v)}
            data = {k: _form_value(v) for k, v in fields.items() if not _is_file(v)}
        else:
            headers["content-type"] = "application/json"
            clean = _jsonable_body(body)
            content = json.dumps(clean, separators=(",", ":")).encode("utf-8")

    return PreparedRequest(
        method=method, url=url, headers=headers, content=content, data=data, files=files
    )


def should_retry(response: httpx.Response) -> bool:
    # A replayed response is the stored outcome for this idempotency key; retrying returns it again.
    if response.headers.get("idempotency-replay") == "true":
        return False
    return response.status_code == 429 or response.status_code >= 500


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
