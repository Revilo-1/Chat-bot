"""
Bot logic module - stateless functions for handling updates
Used by Vercel webhook endpoints
"""

import os
import json
import logging
from datetime import datetime, timedelta
import base64
import pickle

import anthropic
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from supabase import create_client, Client

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None

logger = logging.getLogger(__name__)

# ── Miljøvariabler ────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()
AUTHORIZED_USER_ID = int(os.getenv("AUTHORIZED_USER_ID", "0"))

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "").strip()

GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON", "").strip()
GOOGLE_TOKEN_PICKLE_BASE64 = os.getenv("GOOGLE_TOKEN_PICKLE_BASE64", "").strip()

COPENHAGEN_TZ = ZoneInfo("Europe/Copenhagen") if ZoneInfo else None

# ── Initalisering ─────────────────────────────────────────────────────────────
anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

SCOPES = ["https://www.googleapis.com/auth/calendar"]

DANISH_DAYS = {
    "Monday": "Mandag",
    "Tuesday": "Tirsdag",
    "Wednesday": "Onsdag",
    "Thursday": "Torsdag",
    "Friday": "Fredag",
    "Saturday": "Lørdag",
    "Sunday": "Søndag",
}

VALID_PRIORITIES = {"low", "medium", "high"}


# ── Hjælpefunktioner ──────────────────────────────────────────────────────────

def now_local() -> datetime:
    if COPENHAGEN_TZ:
        return datetime.now(COPENHAGEN_TZ)
    return datetime.now()


def get_chat_session(user_id: int) -> list:
    """Henter chat-session fra Supabase"""
    try:
        res = supabase.table("chat_sessions").select("messages").eq("user_id", user_id).execute()
        if res.data:
            return res.data[0]["messages"] or []
        return []
    except Exception as e:
        logger.error(f"Fejl ved hentning af session: {e}")
        return []


def save_chat_session(user_id: int, messages: list):
    """Gemmer chat-session i Supabase"""
    try:
        supabase.table("chat_sessions").upsert({
            "user_id": user_id,
            "messages": messages,
            "updated_at": now_local().isoformat()
        }).execute()
    except Exception as e:
        logger.error(f"Fejl ved gemning af session: {e}")


# ── Google Calendar ───────────────────────────────────────────────────────────

def load_google_token_from_env():
    raw = GOOGLE_TOKEN_PICKLE_BASE64
    if not raw:
        return None
    try:
        return pickle.loads(base64.b64decode(raw))
    except Exception as e:
        raise RuntimeError("GOOGLE_TOKEN_PICKLE_BASE64 er ugyldig.") from e


def get_google_oauth_flow():
    if GOOGLE_CREDENTIALS_JSON:
        try:
            client_config = json.loads(GOOGLE_CREDENTIALS_JSON)
            return InstalledAppFlow.from_client_config(client_config, SCOPES)
        except json.JSONDecodeError as e:
            raise RuntimeError("GOOGLE_CREDENTIALS_JSON er ikke gyldig JSON.") from e
    raise RuntimeError("Mangler Google credentials (GOOGLE_CREDENTIALS_JSON).")


def get_calendar_service():
    creds = load_google_token_from_env()

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = get_google_oauth_flow()
            creds = flow.run_local_server(port=0)

    return build("calendar", "v3", credentials=creds)


def get_todays_events() -> str:
    """Henter dagens aftaler fra Google Calendar."""
    service = get_calendar_service()
    now = datetime.now()
    start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat() + "+02:00"
    end_of_day = now.replace(hour=23, minute=59, second=59, microsecond=0).isoformat() + "+02:00"

    result = service.events().list(
        calendarId="primary",
        timeMin=start_of_day,
        timeMax=end_of_day,
        singleEvents=True,
        orderBy="startTime"
    ).execute()

    events = result.get("items", [])
    if not events:
        return "Ingen aftaler i dag."

    lines = []
    for e in events:
        start = e["start"].get("dateTime", e["start"].get("date", ""))
        if "T" in start:
            tid = datetime.fromisoformat(start).strftime("%H:%M")
        else:
            tid = "Heldagsbegivenhed"
        lines.append(f"• {tid} — {e.get('summary', '(ingen titel)')}")
    return "\n".join(lines)


def get_week_events() -> str:
    """Henter aftaler for de næste 7 dage, grupperet per dag."""
    service = get_calendar_service()
    now = datetime.now()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=7)

    result = service.events().list(
        calendarId="primary",
        timeMin=start.isoformat() + "+02:00",
        timeMax=end.isoformat() + "+02:00",
        singleEvents=True,
        orderBy="startTime"
    ).execute()

    events = result.get("items", [])
    if not events:
        return "Ingen aftaler de næste 7 dage."

    by_day: dict[str, list[str]] = {}
    for e in events:
        raw_start = e["start"].get("dateTime", e["start"].get("date", ""))
        if "T" in raw_start:
            dt = datetime.fromisoformat(raw_start)
            day_key = dt.strftime("%Y-%m-%d")
            tid = dt.strftime("%H:%M")
        else:
            day_key = raw_start
            tid = "Heldagsbegivenhed"

        if day_key not in by_day:
            by_day[day_key] = []
        by_day[day_key].append(f"  • {tid} — {e.get('summary', '(ingen titel)')}")

    lines = []
    for day_key in sorted(by_day.keys()):
        dt = datetime.strptime(day_key, "%Y-%m-%d")
        danish_day = DANISH_DAYS.get(dt.strftime("%A"), dt.strftime("%A"))
        header = f"*{danish_day} {dt.day}. {dt.strftime('%b')}*"
        lines.append(header)
        lines.extend(by_day[day_key])
        lines.append("")
    return "\n".join(lines).strip()


def create_calendar_event(title: str, start_iso: str, end_iso: str, description: str = "") -> str:
    """Opretter en aftale i Google Calendar."""
    service = get_calendar_service()
    event = {
        "summary": title,
        "description": description,
        "start": {"dateTime": start_iso, "timeZone": "Europe/Copenhagen"},
        "end": {"dateTime": end_iso, "timeZone": "Europe/Copenhagen"},
    }
    result = service.events().insert(calendarId="primary", body=event).execute()
    return result.get("htmlLink", "")


def delete_calendar_event(search_term: str) -> str:
    """Sletter en aftale fra Google Calendar."""
    service = get_calendar_service()
    now = datetime.now()
    end = now + timedelta(days=30)

    result = service.events().list(
        calendarId="primary",
        timeMin=now.isoformat() + "+02:00",
        timeMax=end.isoformat() + "+02:00",
        singleEvents=True,
        orderBy="startTime",
        q=search_term,
    ).execute()

    events = result.get("items", [])
    if not events:
        return f"Fandt ingen aftale med '{search_term}' i de næste 30 dage."

    event = events[0]
    service.events().delete(calendarId="primary", eventId=event["id"]).execute()
    title = event.get("summary", "(ingen titel)")
    raw_start = event["start"].get("dateTime", event["start"].get("date", ""))
    if "T" in raw_start:
        tid = datetime.fromisoformat(raw_start).strftime("%d/%m kl. %H:%M")
    else:
        tid = raw_start
    return f"✅ Slettet: *{title}* ({tid})"


# ── Opgavehåndtering ──────────────────────────────────────────────────────────

def normalize_priority(value: str | None) -> str:
    if not value:
        return "medium"
    p = str(value).strip().lower()
    return p if p in VALID_PRIORITIES else "medium"


def parse_due_date(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return datetime.strptime(str(value).strip(), "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        return None


def normalize_tags(value) -> list[str]:
    if not value:
        return []
    if isinstance(value, list):
        tags = [str(v).strip().lower() for v in value if str(v).strip()]
        return list(dict.fromkeys(tags))
    if isinstance(value, str):
        parts = [p.strip().lower() for p in value.split(",") if p.strip()]
        return list(dict.fromkeys(parts))
    return []


def add_task(user_id: int, payload: dict | str) -> str:
    """Tilføjer en opgave til Supabase."""
    if isinstance(payload, str):
        payload = {"text": payload}

    text = str(payload.get("text", "")).strip()
    if not text:
        return "⚠️ Kunne ikke tilføje opgaven. Mangler tekst."

    due_date = parse_due_date(payload.get("due_date"))
    if payload.get("due_date") and not due_date:
        return "⚠️ Ugyldig frist. Brug format YYYY-MM-DD, fx 2026-04-20."

    try:
        supabase.table("tasks").insert({
            "user_id": user_id,
            "text": text,
            "priority": normalize_priority(payload.get("priority")),
            "due_date": due_date,
            "tags": normalize_tags(payload.get("tags")),
            "created_at": now_local().isoformat(),
        }).execute()
        return f"✅ Opgave tilføjet: _{text}_"
    except Exception as e:
        logger.error(f"Fejl ved tilføjelse af opgave: {e}")
        return "⚠️ Kunne ikke tilføje opgaven."


def list_tasks(user_id: int, status: str = "active") -> str:
    """Viser opgaver fra Supabase."""
    try:
        res = supabase.table("tasks").select("*").eq("user_id", user_id).execute()
        tasks = res.data or []

        if status == "active":
            tasks = [t for t in tasks if not t["done"]]
        elif status == "done":
            tasks = [t for t in tasks if t["done"]]

        if not tasks:
            return "Du har ingen opgaver endnu."

        tasks.sort(key=lambda x: (x["done"], x.get("due_date") or "9999-12-31"))
        lines = []
        for i, t in enumerate(tasks, 1):
            mark = "☑️" if t["done"] else "☐"
            priority_map = {"high": "🔴", "medium": "🟡", "low": "🟢"}
            p = priority_map.get(t.get("priority", "medium"), "🟡")
            due = f" | frist: {t['due_date']}" if t.get("due_date") else ""
            tags = f" | tags: {', '.join(t.get('tags', []))}" if t.get("tags") else ""
            lines.append(f"{mark} {i}. {p} {t['text']}{due}{tags}")
        return "\n".join(lines)
    except Exception as e:
        logger.error(f"Fejl ved hentning af opgaver: {e}")
        return "⚠️ Kunne ikke hente opgaverne."


def complete_task(user_id: int, text: str) -> str:
    """Markerer en opgave som færdig."""
    try:
        search = text.strip().lower()
        res = supabase.table("tasks").select("*").eq("user_id", user_id).eq("done", False).execute()
        tasks = res.data or []

        for t in tasks:
            if t["text"].lower() == search or search in t["text"].lower():
                supabase.table("tasks").update({
                    "done": True,
                    "completed_at": now_local().isoformat()
                }).eq("id", t["id"]).execute()
                return f"☑️ Opgave markeret som færdig: _{t['text']}_"

        return f"Fandt ingen opgave der matcher '{text}'."
    except Exception as e:
        logger.error(f"Fejl ved markering af opgave: {e}")
        return "⚠️ Kunne ikke markere opgaven."


# ── Claude AI ─────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = f"""Du er en personlig kalenderassistent der hjælper med aftaler og opgaver.

I dag er det: {datetime.now().strftime("%A %d. %B %Y, kl. %H:%M")} (dansk tid, Copenhagen).

Du har følgende handlinger til rådighed. Brug tags præcist som vist:

1. DAGSOVERBLIK: <GET_TODAY></GET_TODAY>
2. UGEOVERBLIK: <GET_WEEK></GET_WEEK>
3. OPRET AFTALE: <CREATE_EVENT>{{"title": "Møde", "start": "2026-04-16T10:00:00", "end": "2026-04-16T11:00:00"}}</CREATE_EVENT>
4. SLET AFTALE: <DELETE_EVENT>{{"search": "tandlæge"}}</DELETE_EVENT>
5. VIS OPGAVER: <GET_TASKS></GET_TASKS>
6. TILFØJ OPGAVE: <ADD_TASK>{{"text": "Ring til banken", "priority": "high"}}</ADD_TASK>
7. FULDFØR OPGAVE: <COMPLETE_TASK>{{"text": "Ring til banken"}}</COMPLETE_TASK>

Svar altid på dansk. Vær venlig og kortfattet. Brug kun ét tag per svar.
"""


async def process_with_claude(user_id: int, user_message: str):
    """Sender brugerens besked til Claude og returnerer parsed handlinger."""
    history = get_chat_session(user_id)
    history.append({"role": "user", "content": user_message})

    # Begræns historik til seneste 20 beskeder
    history = history[-20:]

    response = anthropic_client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1000,
        system=SYSTEM_PROMPT,
        messages=history,
    )

    reply_text = response.content[0].text
    history.append({"role": "assistant", "content": reply_text})
    save_chat_session(user_id, history)

    # Parse alle tags
    actions = {
        "get_today": "<GET_TODAY>" in reply_text,
        "get_week": "<GET_WEEK>" in reply_text,
        "create_event": None,
        "delete_event": None,
        "get_tasks": "<GET_TASKS>" in reply_text,
        "add_task": None,
        "complete_task": None,
    }

    def extract_json(tag: str) -> dict | None:
        open_tag = f"<{tag}>"
        close_tag = f"</{tag}>"
        if open_tag in reply_text and close_tag in reply_text:
            try:
                raw = reply_text.split(open_tag)[1].split(close_tag)[0].strip()
                return json.loads(raw)
            except (json.JSONDecodeError, IndexError):
                return None
        return None

    actions["create_event"] = extract_json("CREATE_EVENT")
    actions["delete_event"] = extract_json("DELETE_EVENT")
    actions["add_task"] = extract_json("ADD_TASK")
    actions["complete_task"] = extract_json("COMPLETE_TASK")

    # Rens reply_text for alle tags
    for tag in ["GET_TODAY", "GET_WEEK", "GET_TASKS", "CREATE_EVENT", "DELETE_EVENT", "ADD_TASK", "COMPLETE_TASK"]:
        open_t = f"<{tag}>"
        close_t = f"</{tag}>"
        if open_t in reply_text and close_t in reply_text:
            start_idx = reply_text.find(open_t)
            end_idx = reply_text.find(close_t) + len(close_t)
            reply_text = (reply_text[:start_idx] + reply_text[end_idx:]).strip()

    return reply_text, actions


async def handle_telegram_update(update_data: dict) -> str:
    """Håndterer en Telegram update fra webhook."""
    if "message" not in update_data:
        return "OK"

    message = update_data["message"]
    user_id = message["from"]["id"]
    text = message.get("text", "")

    if user_id != AUTHORIZED_USER_ID:
        return "Unauthorized"

    try:
        reply, actions = await process_with_claude(user_id, text)

        # Udfør handlinger
        if actions["get_today"]:
            try:
                events_text = get_todays_events()
                reply = f"📅 *Dine aftaler i dag:*\n\n{events_text}"
            except Exception as e:
                logger.error(f"Fejl ved aftaler: {e}")
                reply = "⚠️ Kunne ikke hente dine aftaler."

        elif actions["get_week"]:
            try:
                week_text = get_week_events()
                reply = f"📅 *Din uge:*\n\n{week_text}"
            except Exception as e:
                logger.error(f"Fejl ved uge: {e}")
                reply = "⚠️ Kunne ikke hente ugeoversigten."

        elif actions["create_event"]:
            try:
                event_data = actions["create_event"]
                link = create_calendar_event(
                    title=event_data["title"],
                    start_iso=event_data["start"],
                    end_iso=event_data["end"],
                )
                reply = f"✅ *{event_data['title']}* oprettet!\n[Åbn]({link})"
            except Exception as e:
                logger.error(f"Fejl ved aftale: {e}")
                reply = "⚠️ Kunne ikke oprette aftalen."

        elif actions["delete_event"]:
            try:
                result_msg = delete_calendar_event(actions["delete_event"].get("search", ""))
                reply = result_msg
            except Exception as e:
                logger.error(f"Fejl ved sletning: {e}")
                reply = "⚠️ Kunne ikke slette aftalen."

        elif actions["get_tasks"]:
            reply = f"📋 *Dine opgaver:*\n\n{list_tasks(user_id, status='active')}"

        elif actions["add_task"]:
            reply = add_task(user_id, actions["add_task"] or {})

        elif actions["complete_task"]:
            task_text = actions["complete_task"].get("text", "")
            reply = complete_task(user_id, task_text) if task_text else "⚠️ Mangler opgave-tekst."

        return reply

    except Exception as e:
        logger.error(f"Fejl: {e}")
        return "⚠️ Noget gik galt."
