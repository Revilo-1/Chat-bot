"""
Vercel endpoint to configure Telegram webhook for this deployment.

Usage:
  GET /api/set-webhook?secret=YOUR_SETUP_SECRET
"""

import json
import os
from http.server import BaseHTTPRequestHandler
from urllib import parse, request as urlrequest


def _json_response(handler: BaseHTTPRequestHandler, status: int, payload: dict):
    body = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _build_base_url(handler: BaseHTTPRequestHandler) -> str:
    proto = handler.headers.get("x-forwarded-proto", "https")
    host = handler.headers.get("host", "")
    return f"{proto}://{host}".rstrip("/")


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        telegram_token = os.getenv("TELEGRAM_TOKEN", "").strip()
        setup_secret = os.getenv("WEBHOOK_SETUP_SECRET", "").strip()

        if not telegram_token:
            _json_response(
                self,
                500,
                {"ok": False, "error": "TELEGRAM_TOKEN mangler i environment variables."},
            )
            return

        query = parse.parse_qs(parse.urlsplit(self.path).query)
        provided_secret = (query.get("secret") or [""])[0].strip()

        if setup_secret and provided_secret != setup_secret:
            _json_response(
                self,
                401,
                {"ok": False, "error": "Ugyldig secret. Tilføj ?secret=..."},
            )
            return

        webhook_url = f"{_build_base_url(self)}/api/telegram"
        endpoint = (
            f"https://api.telegram.org/bot{telegram_token}/setWebhook"
            f"?url={parse.quote(webhook_url, safe=':/')}"
            "&drop_pending_updates=true"
        )

        try:
            with urlrequest.urlopen(endpoint, timeout=15) as resp:
                raw = resp.read().decode("utf-8")
            telegram_result = json.loads(raw)
        except Exception as e:
            _json_response(self, 500, {"ok": False, "error": f"Telegram request fejl: {e}"})
            return

        if not telegram_result.get("ok"):
            _json_response(
                self,
                502,
                {
                    "ok": False,
                    "error": "Telegram afviste webhook.",
                    "telegram": telegram_result,
                    "webhook_url": webhook_url,
                },
            )
            return

        _json_response(
            self,
            200,
            {
                "ok": True,
                "message": "Webhook sat korrekt.",
                "webhook_url": webhook_url,
                "telegram": telegram_result,
            },
        )
