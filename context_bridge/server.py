"""HTTP server: JSON API + static web console + SSE change feed.

Standard library only (``http.server.ThreadingHTTPServer``). Endpoints are specified in
``API.md``; the console lives in ``web/``. Contract (implemented by the API owner):

    server = create_server(config, hub=hub)   # -> ThreadingHTTPServer, bound but not started
    serve(config)                              # blocking serve_forever, prints URL
    run_in_thread(config, hub)                 # for tests/embedded use

Requirements:

- Path traversal protection for static files; unknown non-API paths fall back to index.html.
- Optional bearer token (``server.token``) enforced on ``/api/*`` and HTML when set.
- SSE clients survive hub updates; send comment heartbeats; clean up on disconnect.
- One sync thread at a time for ``POST /api/sync``; never block the API thread on a sync.
- JSON errors as ``{"error": {"code", "message"}}`` with proper status codes.
"""

from __future__ import annotations


def create_server(config, hub=None):
    raise NotImplementedError("server.create_server is implemented by the API owner")


def serve(config) -> None:
    raise NotImplementedError("server.serve is implemented by the API owner")
