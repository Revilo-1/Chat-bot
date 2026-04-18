"""
Vercel API endpoint for Telegram webhook
"""
import json
import sys
import os
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot_logic import handle_telegram_update

class handler(BaseHTTPRequestHandler):
    """Telegram webhook handler for Vercel Python runtime."""

    def _send_json(self, status_code: int, payload: dict):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._send_json(200, {"ok": True, "message": "telegram webhook endpoint"})

    def do_POST(self):
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(content_length) if content_length > 0 else b"{}"
            data = json.loads(raw.decode("utf-8") or "{}")

            import asyncio

            reply = asyncio.run(handle_telegram_update(data))
            self._send_json(200, {"ok": True, "result": reply})
        except Exception as e:
            print(f"Webhook error: {e}")
            import traceback

            traceback.print_exc()
            self._send_json(500, {"ok": False, "error": str(e)})
