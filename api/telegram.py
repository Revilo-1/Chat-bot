"""
Vercel API endpoint for Telegram webhook
"""
import json
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot_logic import handle_telegram_update

def handler(request):
    """Telegram webhook handler for Vercel"""
    if request.method == "POST":
        try:
            data = request.json()
            # Run async function synchronously
            import asyncio
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            reply = loop.run_until_complete(handle_telegram_update(data))
            return {"statusCode": 200, "body": json.dumps({"ok": True, "result": reply})}
        except Exception as e:
            print(f"Webhook error: {e}")
            import traceback
            traceback.print_exc()
            return {"statusCode": 500, "body": json.dumps({"ok": False, "error": str(e)})}

    return {"statusCode": 405, "body": "Method not allowed"}
