"""Local HUD dashboard server.

Phase 6 rules:
- Reads and acts only through existing public interfaces.
- No model loading, no direct tool execution bypass.
- Approvals go through app.approve/app.deny (same approval gate).
- Local-only binding by default with authentication.
- Optional secure phone access only when explicitly enabled and configured.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import threading
import time
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from ..safety.redaction import redact_text, redact_value
from .ui import DASHBOARD_HTML


class DashboardAuthError(RuntimeError):
    pass


class DashboardServer:
    def __init__(self, app: Any, *, config: dict | None = None) -> None:
        self.app = app
        cfg = dict(config or app.config.get("dashboard") or {})
        self.enabled = bool(cfg.get("enabled", True))
        self.host = str(cfg.get("host", "127.0.0.1"))
        self.port = int(cfg.get("port", 8765))
        self.auth_enabled = bool(cfg.get("auth_enabled", True))
        self.token_env = str(cfg.get("token_env", "OMEN_DASHBOARD_TOKEN"))
        self.token = self._resolve_token(cfg)
        self.allow_remote = bool(cfg.get("allow_remote", False))
        self.remote_read_only = bool(cfg.get("remote_read_only", True))
        self.tls_cert = cfg.get("tls_cert")
        self.tls_key = cfg.get("tls_key")
        self._validate_bind()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._started = threading.Event()

    def _resolve_token(self, cfg: dict) -> str | None:
        env_name = str(cfg.get("token_env") or "OMEN_DASHBOARD_TOKEN")
        env = {}
        env_path = Path(getattr(self.app, "root", Path.cwd())) / ".env"
        if env_path.exists():
            for line in env_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                v = v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                    v = v[1:-1]
                env[k.strip()] = v
        import os
        token = os.environ.get(env_name) or env.get(env_name) or cfg.get("token")
        if self.auth_enabled and not token:
            raise DashboardAuthError(
                f"Dashboard auth is enabled but no token is configured; set {env_name} in .env"
            )
        return str(token) if token else None

    def _validate_bind(self) -> None:
        if self.host not in ("127.0.0.1", "localhost", "::1") and not self.allow_remote:
            raise DashboardAuthError(
                "Dashboard remote bind requires dashboard.allow_remote=true and explicit TLS configuration"
            )
        if self.allow_remote:
            if not self.tls_cert or not self.tls_key:
                raise DashboardAuthError(
                    "Remote dashboard access requires dashboard.tls_cert and dashboard.tls_key"
                )
            cert = Path(str(self.tls_cert))
            key = Path(str(self.tls_key))
            if not cert.exists() or not key.exists():
                raise DashboardAuthError("Remote dashboard TLS cert/key paths must exist")

    @property
    def url(self) -> str:
        scheme = "https" if self.allow_remote else "http"
        host = "127.0.0.1" if self.host in ("127.0.0.1", "localhost") else self.host
        return f"{scheme}://{host}:{self.port}"

    def status(self) -> dict:
        return {
            "enabled": self.enabled,
            "running": self._server is not None,
            "url": self.url if self._server is not None else None,
            "host": self.host,
            "port": self.port,
            "auth_enabled": self.auth_enabled,
            "allow_remote": self.allow_remote,
            "remote_read_only": self.remote_read_only,
            "tls": bool(self.allow_remote),
        }

    def start(self) -> dict:
        if not self.enabled:
            return {"started": False, "reason": "dashboard disabled"}
        if self._server is not None:
            return {"started": True, **self.status()}
        server = self._make_server()
        if self.allow_remote:
            import ssl
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(certfile=str(self.tls_cert), keyfile=str(self.tls_key))
            server.socket = context.wrap_socket(server.socket, server_side=True)
        self._server = server
        self.port = int(server.server_address[1])
        self._thread = threading.Thread(target=server.serve_forever, daemon=True, name="omen-dashboard")
        self._thread.start()
        self._started.set()
        self._audit("dashboard_started", "dashboard server started", status="ok")
        return {"started": True, **self.status()}

    def stop(self) -> dict:
        if self._server is None:
            return {"stopped": False, "reason": "not running"}
        server = self._server
        self._server = None
        server.shutdown()
        server.server_close()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)
        self._audit("dashboard_stopped", "dashboard server stopped", status="ok")
        return {"stopped": True}

    def close(self) -> None:
        self.stop()

    def _audit(self, event_type: str, summary: str, *, status: str = "ok", **extra: Any) -> None:
        try:
            self.app.audit.append({"event_type": event_type, "tool": "dashboard", "status": status, "summary": summary, **extra})
        except Exception:
            pass

    def _make_server(self) -> ThreadingHTTPServer:
        dashboard = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "OmenDashboard/1.0"

            def log_message(self, format: str, *args: Any) -> None:  # quiet by default
                return

            def _send(self, status: int, body: bytes, content_type: str = "application/json; charset=utf-8") -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "no-referrer")
                self.end_headers()
                self.wfile.write(body)

            def _json(self, status: int, payload: Any) -> None:
                safe = redact_value(payload)
                body = json.dumps(safe, ensure_ascii=False, default=str).encode("utf-8")
                self._send(status, body)

            def _error(self, status: int, message: str) -> None:
                self._json(status, {"error": redact_text(str(message)), "status": status})

            def _auth_token(self) -> str | None:
                auth = self.headers.get("Authorization", "")
                if auth.lower().startswith("bearer "):
                    return auth.split(None, 1)[1].strip()
                cookie = SimpleCookie(self.headers.get("Cookie", ""))
                if "omen_dashboard_token" in cookie:
                    return cookie["omen_dashboard_token"].value
                parsed = urlparse(self.path)
                qs = parse_qs(parsed.query)
                if "token" in qs and qs["token"]:
                    return qs["token"][0]
                return None

            def _authorized(self) -> bool:
                if not dashboard.auth_enabled:
                    return True
                supplied = self._auth_token()
                if not supplied or not dashboard.token:
                    return False
                return hmac.compare_digest(str(supplied), str(dashboard.token))

            def _read_only_remote(self) -> bool:
                return dashboard.allow_remote and dashboard.remote_read_only

            def _read_json(self) -> dict:
                length = int(self.headers.get("Content-Length", "0") or "0")
                raw = self.rfile.read(length) if length > 0 else b"{}"
                try:
                    data = json.loads(raw.decode("utf-8") or "{}")
                except Exception as exc:
                    raise ValueError(f"Invalid JSON body: {exc}") from exc
                if not isinstance(data, dict):
                    raise ValueError("JSON body must be an object")
                return data

            def _status_payload(self) -> dict:
                status = dashboard.app.status()
                status["settings"] = dashboard._settings_view()
                status["alerts_detail"] = dashboard._alerts_detail()
                return status

            def do_GET(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                path = parsed.path
                if path == "/api/healthz":
                    self._json(200, {"ok": True, "dashboard": dashboard.status()})
                    return
                if not self._authorized():
                    self._error(401, "Unauthorized")
                    return
                if path == "/" or path == "/index.html":
                    self._send(200, DASHBOARD_HTML.encode("utf-8"), "text/html; charset=utf-8")
                    return
                if path == "/api/status":
                    self._json(200, self._status_payload())
                    return
                if path == "/api/approvals":
                    self._json(200, {"approvals": dashboard._pending_approvals()})
                    return
                if path == "/api/memory":
                    self._json(200, {"items": dashboard._memory_items("")})
                    return
                if path == "/api/memory/search":
                    q = parse_qs(parsed.query).get("q", [""])[0]
                    self._json(200, {"items": dashboard._memory_items(q)})
                    return
                if path == "/api/events":
                    self._stream_events()
                    return
                self._error(404, "Not found")

            def do_POST(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                path = parsed.path
                if not self._authorized():
                    self._error(401, "Unauthorized")
                    return
                if self._read_only_remote() and path.startswith("/api/approvals/"):
                    self._error(403, "Remote dashboard is read-only; approval actions are disabled")
                    return
                if path.startswith("/api/approvals/") and path.endswith("/approve"):
                    token_id = path.split("/")[3]
                    try:
                        result = dashboard.app.approve(token_id)
                        dashboard._audit("dashboard_approval", f"approved {token_id} via dashboard", status="ok", target=token_id)
                        self._json(200, {"ok": True, "result": result})
                    except Exception as exc:
                        dashboard._audit("dashboard_approval_error", f"approval failed for {token_id}", status="error", error=str(exc))
                        self._error(400, str(exc))
                    return
                if path.startswith("/api/approvals/") and path.endswith("/deny"):
                    token_id = path.split("/")[3]
                    try:
                        result = dashboard.app.deny(token_id)
                        dashboard._audit("dashboard_deny", f"denied {token_id} via dashboard", status="ok", target=token_id)
                        self._json(200, {"ok": True, "result": result})
                    except Exception as exc:
                        dashboard._audit("dashboard_approval_error", f"deny failed for {token_id}", status="error", error=str(exc))
                        self._error(400, str(exc))
                    return
                self._error(404, "Not found")

            def _stream_events(self) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "keep-alive")
                self.end_headers()
                last_hash = None
                try:
                    for _ in range(300):  # ~5 minutes max per connection; client reconnects
                        events = dashboard.app.history(limit=20)
                        for event in events[-20:]:
                            ident = event.get("event_id") or hashlib.sha256(json.dumps(event, sort_keys=True, default=str).encode()).hexdigest()
                            if ident == last_hash:
                                continue
                            # send only newer tail by remembering last seen in this loop
                        # simpler: send latest event if changed
                        latest = events[-1] if events else None
                        if latest is not None:
                            ident = latest.get("event_id") or json.dumps(latest, sort_keys=True, default=str)
                            if ident != last_hash:
                                payload = json.dumps(redact_value(latest), default=str)
                                self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                                self.wfile.flush()
                                last_hash = ident
                        time.sleep(1)
                except Exception:
                    return

        return ThreadingHTTPServer((self.host, self.port), Handler)

    def _pending_approvals(self) -> list[dict]:
        items = []
        for token in self.app.approvals.list_pending():
            action = dict(getattr(token, "action", {}) or {})
            items.append({
                "token_id": token.token_id,
                "tool": action.get("tool"),
                "target": action.get("target"),
                "risk": action.get("permission_class") or action.get("risk"),
                "expires_at": getattr(token, "expires_at", None),
                "state": getattr(token, "state", "pending"),
            })
        return items

    def _alerts_detail(self) -> list[dict]:
        # public interfaces only: audit/history/alerts
        alerts = []
        try:
            raw = self.app.alerts()
            if isinstance(raw, list):
                alerts = [item if isinstance(item, dict) else {"type": str(item)} for item in raw]
        except Exception as exc:
            alerts = [{"type": "alerts_unavailable", "error": str(exc)}]
        return alerts

    def _memory_items(self, query: str) -> list[dict]:
        try:
            if query:
                items = self.app.memory.recall(query, limit=20)
            elif hasattr(self.app.memory, "recent"):
                items = self.app.memory.recent(limit=20)
            elif hasattr(self.app.memory, "search"):
                items = self.app.memory.search(query="", limit=20)
            else:
                items = self.app.memory.recall("", limit=20)
        except Exception as exc:
            return [{"kind": "error", "text": f"Memory unavailable: {exc}"}]
        out = []
        for item in items[:20]:
            if hasattr(item, "as_dict"):
                item = item.as_dict()
            if not isinstance(item, dict):
                item = {"text": str(item)}
            out.append({
                "id": item.get("id"),
                "kind": item.get("kind") or item.get("type"),
                "text": redact_text(str(item.get("text") or item.get("content") or item.get("summary") or "")),
                "created_at": item.get("created_at") or item.get("timestamp"),
                "author": item.get("author"),
                "source": item.get("source"),
            })
        return out

    def _settings_view(self) -> dict:
        cfg = self.app.config
        return redact_value({
            "dashboard_enabled": bool(cfg.get("dashboard.enabled", True)),
            "dashboard_host": cfg.get("dashboard.host", "127.0.0.1"),
            "dashboard_port": cfg.get("dashboard.port", 8765),
            "dashboard_auth": bool(cfg.get("dashboard.auth_enabled", True)),
            "allow_remote": bool(cfg.get("dashboard.allow_remote", False)),
            "remote_read_only": bool(cfg.get("dashboard.remote_read_only", True)),
            "cost_monthly_budget_usd": cfg.get("cost.monthly_budget_usd"),
            "cost_warn_at_percent": cfg.get("cost.warn_at_percent"),
            "degradation_order": cfg.get("resources.degradation_order"),
        })
