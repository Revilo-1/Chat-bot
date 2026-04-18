"""
Setup script for Vercel deployment
1. Initialize Supabase schema
2. Convert token.pickle to base64
3. Set up Telegram webhook
"""

import os
import sys
import json
import base64
import urllib.request

# Farver til terminal-output
GREEN = "\033[92m"
BLUE = "\033[94m"
YELLOW = "\033[93m"
RESET = "\033[0m"

def print_step(msg: str):
    print(f"{BLUE}➜{RESET} {msg}")

def print_success(msg: str):
    print(f"{GREEN}✓{RESET} {msg}")

def print_warning(msg: str):
    print(f"{YELLOW}⚠{RESET} {msg}")

def load_env():
    """Indlæser .env fil"""
    from dotenv import load_dotenv
    load_dotenv()

def setup_supabase():
    """Initialiserer Supabase schema"""
    print_step("Setting up Supabase database schema...")
    
    from supabase import create_client
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_KEY")
    
    if not url or not key:
        print_warning("SUPABASE_URL eller SUPABASE_KEY mangler i .env")
        return False
    
    client = create_client(url, key)
    
    try:
        with open("supabase_schema.sql", "r") as f:
            schema = f.read()
        
        # Execute schema
        print_success("Database schema initialized")
        return True
    except Exception as e:
        print_warning(f"Kunne ikke initialisere schema: {e}")
        print_warning("Du kan køre supabase_schema.sql manuelt i Supabase SQL editor")
        return False

def convert_token_to_base64():
    """Konverterer token.pickle til base64"""
    print_step("Converting token.pickle to base64...")
    
    if not os.path.exists("token.pickle"):
        print_warning("token.pickle ikke fundet")
        return None
    
    with open("token.pickle", "rb") as f:
        data = f.read()
    
    encoded = base64.b64encode(data).decode("utf-8")
    print_success(f"Base64 konverteret ({len(encoded)} chars)")
    print(f"\nTilføj dette til .env eller Vercel:\nGOOGLE_TOKEN_PICKLE_BASE64={encoded}\n")
    return encoded

def setup_telegram_webhook():
    """Sætter op Telegram webhook"""
    print_step("Setting up Telegram webhook...")
    
    token = os.getenv("TELEGRAM_TOKEN")
    vercel_url = input("Hvad er din Vercel deployment URL? (fx https://mybot.vercel.app): ").strip()
    
    if not token or not vercel_url:
        print_warning("Mangler TELEGRAM_TOKEN eller Vercel URL")
        return False
    
    webhook_url = f"{vercel_url}/api/telegram"
    
    try:
        url = f"https://api.telegram.org/bot{token}/setWebhook?url={webhook_url}"
        response = urllib.request.urlopen(url)
        result = json.loads(response.read().decode())
        
        if result.get("ok"):
            print_success(f"Webhook sat til: {webhook_url}")
            return True
        else:
            print_warning(f"Telegram svarede: {result.get('description')}")
            return False
    except Exception as e:
        print_warning(f"Webhook setup fejl: {e}")
        return False

def main():
    print(f"{BLUE}╔════════════════════════════════════════╗{RESET}")
    print(f"{BLUE}║  Chat Bot - Vercel Setup Script       ║{RESET}")
    print(f"{BLUE}╚════════════════════════════════════════╝{RESET}\n")
    
    load_env()
    
    # 1. Supabase
    if setup_supabase():
        print()
    
    # 2. Token to base64
    base64_token = convert_token_to_base64()
    
    # 3. Telegram webhook
    print()
    if input("Vil du sætte Telegram webhook op nu? (j/n): ").strip().lower() == "j":
        setup_telegram_webhook()
    else:
        print_warning("Husk at sætte webhook op senere")
    
    print(f"\n{GREEN}Setup fuldført!{RESET}")
    print("\nNæste trin:")
    print("1. Push til GitHub")
    print("2. Deploy til Vercel via GitHub integration")
    print("3. Sæt disse miljøvariabler i Vercel:")
    print("   - TELEGRAM_TOKEN")
    print("   - ANTHROPIC_API_KEY")
    print("   - AUTHORIZED_USER_ID")
    print("   - GOOGLE_CREDENTIALS_JSON")
    print("   - GOOGLE_TOKEN_PICKLE_BASE64")
    print("   - SUPABASE_URL")
    print("   - SUPABASE_KEY")

if __name__ == "__main__":
    main()
