"""
Vercel API endpoint for Telegram webhook
"""
import json
import asyncio
from bot_logic import handle_telegram_update, AUTHORIZED_USER_ID

async def handler(request):
    """Telegram webhook handler for Vercel"""
    if request.method == "POST":
        try:
            data = await request.json()
            reply = await handle_telegram_update(data)
            return {"statusCode": 200, "body": json.dumps({"ok": True, "result": reply})}
        except Exception as e:
            print(f"Webhook error: {e}")
            return {"statusCode": 500, "body": json.dumps({"ok": False, "error": str(e)})}

    return {"statusCode": 405, "body": "Method not allowed"}
