"""The impure sliver: an HTTP transport backed by requests.

Everything interesting about the live adapter (URLs, headers, error
mapping) lives in :mod:`ghl_bridge.live.mapping` under strict typing and
offline tests. This module is the smallest possible wrapper around a real
HTTP client, kept behind the optional ``[live]`` extra so the core package
installs and runs with pydantic alone.
"""

from __future__ import annotations

from ..live.mapping import HttpResponse, PlannedRequest, auth_headers


class RequestsTransport:
    """Sends a :class:`PlannedRequest` with the requests library.

    Instantiating without the extra installed fails at construction with
    the install command in the message, not at the first call in
    production.
    """

    def __init__(self, *, token: str, timeout_seconds: float = 30.0) -> None:
        try:
            import requests
        except ImportError as exc:
            raise ImportError(
                'the live transport needs the optional extra: pip install "ghl-bridge[live]"'
            ) from exc
        self._requests = requests
        self._token = token
        self._timeout = timeout_seconds

    def send(self, request: PlannedRequest) -> HttpResponse:
        response = self._requests.request(
            request.method,
            request.url,
            headers=auth_headers(self._token, request.version),
            params=request.params or None,
            json=request.json_body,
            timeout=self._timeout,
        )
        try:
            body = response.json()
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            body = {"data": body}
        return HttpResponse(
            status=response.status_code,
            headers={str(k): str(v) for k, v in response.headers.items()},
            body=body,
        )
