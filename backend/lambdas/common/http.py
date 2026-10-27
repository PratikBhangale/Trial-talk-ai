"""Tiny router and response helpers for API Gateway HTTP API (payload format 2.0)."""

import json
import re
import traceback
from typing import Callable

from common import db, telemetry


class HttpError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def response(status: int, body=None) -> dict:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json", "Cache-Control": "no-store"},
        "body": json.dumps(body if body is not None else {}, default=str),
    }


def json_body(event: dict) -> dict:
    raw = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        import base64
        raw = base64.b64decode(raw).decode()
    try:
        body = json.loads(raw)
    except ValueError:
        raise HttpError(400, "Body must be JSON")
    if not isinstance(body, dict):
        raise HttpError(400, "Body must be a JSON object")
    return body


def query_params(event: dict) -> dict:
    return event.get("queryStringParameters") or {}


class Router:
    """Routes '{METHOD} /path/{param}' to handler(event, claims, **params)."""

    def __init__(self):
        self.routes: list[tuple[str, re.Pattern, Callable, str]] = []

    def route(self, method: str, path: str):
        pattern = re.compile("^" + re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", path) + "$")

        def register(fn):
            self.routes.append((method, pattern, fn, path))
            return fn
        return register

    def include(self, other: "Router") -> None:
        """Serve another module's routes from this router (one Lambda, several files)."""
        self.routes += other.routes

    def dispatch(self, event: dict, claims_fn: Callable[[dict], dict]) -> dict:
        method = event.get("requestContext", {}).get("http", {}).get("method", "GET")
        path = event.get("rawPath", "/")
        template = next((tpl for m, pattern, _, tpl in self.routes if m == method and pattern.match(path)),
                        "unmatched")  # the route template, never the URL: ids stay out of telemetry
        with telemetry.request_span(method, template, event.get("headers")) as span:
            resp = self._dispatch(event, claims_fn, method, path)
            telemetry.finish_request(span, resp["statusCode"])
            return resp

    def _dispatch(self, event: dict, claims_fn: Callable[[dict], dict], method: str, path: str) -> dict:
        try:
            path_matched = False
            for m, pattern, fn, _ in self.routes:
                match = pattern.match(path)
                if not match:
                    continue
                path_matched = True
                if m == method:
                    claims = claims_fn(event)
                    status, body = fn(event, claims, **match.groupdict())
                    return response(status, body)
            raise HttpError(405 if path_matched else 404, "Method not allowed" if path_matched else "Not found")
        except HttpError as e:
            return response(e.status, {"error": e.message})
        except Exception as e:
            if db.is_unique_violation(e):
                return response(409, {"error": "A record with that value already exists"})
            if "row-level security" in str(e) or "permission denied" in str(e):
                return response(403, {"error": "Forbidden"})
            traceback.print_exc()
            return response(500, {"error": "Internal error"})
