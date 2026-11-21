"""Compose-only webhook receiver for the dev stack and the e2e runner (SC5, D-98).

Records every request a card's webhook delivery makes and answers 200, or a queued failure status
so the e2e journey can prove retries. Test-only mutators: ``POST /_sink/fail`` queues failures,
``POST /_sink/reset`` clears everything, ``GET /_sink/requests`` lists what arrived. It runs on the
identity image (FastAPI, uvicorn, stdlib) and imports nothing from the platform. It sits on the
compose backend network only: no published port and no gateway route, so the mutators are never
reachable from outside.

Request bodies are kept in memory for the test to verify the signature and are never logged, and
the sink holds no secret of its own.
"""

from __future__ import annotations

import base64
import threading
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

MAX_RECORDED = 500

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
_lock = threading.Lock()
_requests: list[dict[str, Any]] = []
_failures: dict[str, int] = {"count": 0, "status": 500}


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/_sink/requests")
async def recorded() -> list[dict[str, Any]]:
    with _lock:
        return list(_requests)


@app.post("/_sink/fail")
async def fail_next(request: Request) -> Response:
    body = await request.json()
    count = int(body.get("count", 0))
    status = int(body.get("status", 500))
    if count < 0 or not 400 <= status <= 599:
        return JSONResponse({"detail": "count >= 0 and 400 <= status <= 599"}, status_code=422)
    with _lock:
        _failures["count"] = count
        _failures["status"] = status
        return JSONResponse(dict(_failures))


@app.post("/_sink/reset")
async def reset() -> dict[str, str]:
    with _lock:
        _requests.clear()
        _failures["count"] = 0
        _failures["status"] = 500
    return {"status": "reset"}


@app.post("/{path:path}")
async def receive(path: str, request: Request) -> Response:
    body = await request.body()
    entry = {
        "path": "/" + path,
        "headers": {key.lower(): value for key, value in request.headers.items()},
        "body_b64": base64.b64encode(body).decode("ascii"),
        "received_at": datetime.now(UTC).isoformat(),
    }
    with _lock:
        _requests.append(entry)
        del _requests[:-MAX_RECORDED]
        if _failures["count"] > 0:
            _failures["count"] -= 1
            return JSONResponse({"detail": "queued failure"}, status_code=_failures["status"])
    return JSONResponse({"status": "received"}, status_code=200)
