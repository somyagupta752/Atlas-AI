"""Request context: a request ID on every request, one structured access log line, request metrics."""

import logging
import time

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core import metrics
from app.core.logging import (
    accept_request_id,
    log_event,
    request_id_var,
    workflow_id_var,
)

logger = logging.getLogger("atlas.http")


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {k.decode().lower(): v.decode("latin-1") for k, v in scope["headers"]}
        request_id = accept_request_id(headers.get("x-request-id"))
        token = request_id_var.set(request_id)
        workflow_token = workflow_id_var.set(None)
        started = time.perf_counter()
        status = {"code": 500}

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
                message.setdefault("headers", []).append((b"x-request-id", request_id.encode()))
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            elapsed = time.perf_counter() - started
            route = scope.get("route")
            template = getattr(route, "path", None) or "unmatched"  # never the raw path: it holds IDs
            metrics.HTTP_REQUESTS.labels(scope["method"], template, metrics.status_class(status["code"])).inc()
            metrics.HTTP_LATENCY.labels(scope["method"], template).observe(elapsed)
            log_event(logger, "http_request", method=scope["method"], route=template, status=status["code"],
                      duration_ms=round(elapsed * 1000, 1),
                      level=logging.ERROR if status["code"] >= 500 else logging.INFO)
            request_id_var.reset(token)
            workflow_id_var.reset(workflow_token)
