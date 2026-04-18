"""
Telegram Calendar Bot
- Opret aftaler via naturlig samtale
- Claude API forstår hvad du skriver
- Google Calendar gemmer aftalerne
- Ugeoverblik, slet aftaler, opgaveliste

Krav: pip install python-telegram-bot anthropic google-auth-oauthlib google-api-python-client
"""

import os
import json
import logging
from datetime import datetime, timedelta, time
from urllib import error as urlerror
from urllib import request as urlrequest
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
import anthropic
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
import pickle
import base64

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

# ── Konfiguration ─────────────────────────────────────────────────────────────
def get_required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Manglende miljøvariabel: {name}")
    return value


TELEGRAM_TOKEN = get_required_env("TELEGRAM_TOKEN")
ANTHROPIC_API_KEY = get_required_env("ANTHROPIC_API_KEY")
AUTHORIZED_USER_ID = int(get_required_env("AUTHORIZED_USER_ID"))
INVENTORY_API_URL = os.getenv("INVENTORY_API_URL", "").strip()
INVENTORY_API_TOKEN = os.getenv("INVENTORY_API_TOKEN", "").strip()
INVENTORY_API_TIMEOUT_SECONDS = int(os.getenv("INVENTORY_API_TIMEOUT_SECONDS", "12"))
ENABLE_DAILY_BRIEFING = os.getenv("ENABLE_DAILY_BRIEFING", "false").lower() == "true"
DAILY_BRIEFING_TIME = os.getenv("DAILY_BRIEFING_TIME", "08:15")

SCOPES = ["https://www.googleapis.com/auth/calendar"]
TASKS_FILE = "tasks.json"
# ──────────────────────────────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# Hukommelse per bruger (session)
user_sessions: dict[int, list] = {}

DANISH_DAYS = {
    "Monday": "Mandag",
    "Tuesday": "Tirsdag",
    "Wednesday": "Onsdag",
    "Thursday": "Torsdag",
    "Friday": "Fredag",
    "Saturday": "Lørdag",
    "Sunday": "Søndag",
}

COPENHAGEN_TZ = ZoneInfo("Europe/Copenhagen") if ZoneInfo else None


# ── Google Calendar ────────────────────────────────────────────────────────────

def load_google_token_from_env():
    raw = os.getenv("GOOGLE_TOKEN_PICKLE_BASE64", "").strip()
    if not raw:
        return None
    try:
        return pickle.loads(base64.b64decode(raw))
    except Exception as e:
        raise RuntimeError("GOOGLE_TOKEN_PICKLE_BASE64 er ugyldig.") from e


def get_google_oauth_flow():
    if os.path.exists("credentials.json"):
        return InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)

    raw = os.getenv("GOOGLE_CREDENTIALS_JSON", "").strip()
    if not raw:
        raise RuntimeError(
            "Mangler Google credentials. Tilføj credentials.json eller GOOGLE_CREDENTIALS_JSON."
        )

    try:
        client_config = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError("GOOGLE_CREDENTIALS_JSON er ikke gyldig JSON.") from e

    return InstalledAppFlow.from_client_config(client_config, SCOPES)


def get_calendar_service():
    creds = None
    if os.path.exists("token.pickle"):
        with open("token.pickle", "rb") as f:
            creds = pickle.load(f)
    else:
        creds = load_google_token_from_env()

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = get_google_oauth_flow()
            creds = flow.run_local_server(port=0)
        if os.path.exists("token.pickle") or not os.getenv("GOOGLE_TOKEN_PICKLE_BASE64", "").strip():
            with open("token.pickle", "wb") as f:
                pickle.dump(creds, f)

    return build("calendar", "v3", credentials=creds)


def get_todays_events() -> str:
    """Henter dagens aftaler fra Google Calendar og returnerer dem som tekst."""
    service = get_calendar_service()
    now = datetime.now()
    start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat() + "+02:00"
    end_of_day   = now.replace(hour=23, minute=59, second=59, microsecond=0).isoformat() + "+02:00"

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
    end   = start + timedelta(days=7)

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

    # Grupper per dato
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
    """Opretter en aftale i Google Calendar og returnerer et link."""
    service = get_calendar_service()
    event = {
        "summary": title,
        "description": description,
        "start": {"dateTime": start_iso, "timeZone": "Europe/Copenhagen"},
        "end":   {"dateTime": end_iso,   "timeZone": "Europe/Copenhagen"},
    }
    result = service.events().insert(calendarId="primary", body=event).execute()
    return result.get("htmlLink", "")


def delete_calendar_event(search_term: str) -> str:
    """Finder og sletter den første aftale der matcher søgetermen i de næste 30 dage."""
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
    event_id = event["id"]
    title = event.get("summary", "(ingen titel)")
    raw_start = event["start"].get("dateTime", event["start"].get("date", ""))
    if "T" in raw_start:
        tid = datetime.fromisoformat(raw_start).strftime("%d/%m kl. %H:%M")
    else:
        tid = raw_start

    service.events().delete(calendarId="primary", eventId=event_id).execute()
    return f"✅ Slettet: *{title}* ({tid})"


# ── Lokal opgaveliste ──────────────────────────────────────────────────────────

VALID_PRIORITIES = {"low", "medium", "high"}


def now_local() -> datetime:
    if COPENHAGEN_TZ:
        return datetime.now(COPENHAGEN_TZ)
    return datetime.now()


def normalize_priority(value: str | None) -> str:
    if not value:
        return "medium"
    p = str(value).strip().lower()
    if p in VALID_PRIORITIES:
        return p
    return "medium"


def parse_due_date(value: str | None) -> str | None:
    if not value:
        return None
    raw = str(value).strip()
    try:
        return datetime.strptime(raw, "%Y-%m-%d").strftime("%Y-%m-%d")
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


def normalize_task_record(item: dict) -> dict:
    text = str(item.get("text", "")).strip()
    done = bool(item.get("done", False))
    created_at = str(item.get("created_at", "")).strip() or now_local().isoformat()
    completed_at = str(item.get("completed_at", "")).strip() or None
    if done and not completed_at:
        completed_at = now_local().isoformat()
    if not done:
        completed_at = None
    return {
        "text": text,
        "done": done,
        "priority": normalize_priority(item.get("priority")),
        "due_date": parse_due_date(item.get("due_date")),
        "tags": normalize_tags(item.get("tags")),
        "created_at": created_at,
        "completed_at": completed_at,
    }

def load_tasks() -> list[dict]:
    if not os.path.exists(TASKS_FILE):
        return []
    with open(TASKS_FILE, "r", encoding="utf-8") as f:
        raw = json.load(f)

    if not isinstance(raw, list):
        return []

    tasks = [normalize_task_record(item) for item in raw if isinstance(item, dict)]
    if tasks != raw:
        save_tasks(tasks)
    return tasks


def save_tasks(tasks: list[dict]):
    with open(TASKS_FILE, "w", encoding="utf-8") as f:
        json.dump(tasks, f, ensure_ascii=False, indent=2)


def format_task_line(i: int, task: dict) -> str:
    mark = "☑️" if task["done"] else "☐"
    priority_map = {"high": "🔴", "medium": "🟡", "low": "🟢"}
    p = priority_map.get(task.get("priority", "medium"), "🟡")
    due = f" | frist: {task['due_date']}" if task.get("due_date") else ""
    tags = f" | tags: {', '.join(task.get('tags', []))}" if task.get("tags") else ""
    return f"{mark} {i}. {p} {task['text']}{due}{tags}"


def add_task(payload: dict | str) -> str:
    if isinstance(payload, str):
        payload = {"text": payload}

    text = str(payload.get("text", "")).strip()
    if not text:
        return "⚠️ Kunne ikke tilføje opgaven. Mangler tekst."

    due_date = parse_due_date(payload.get("due_date"))
    if payload.get("due_date") and not due_date:
        return "⚠️ Ugyldig frist. Brug format YYYY-MM-DD, fx 2026-04-20."

    task = {
        "text": text,
        "done": False,
        "priority": normalize_priority(payload.get("priority")),
        "due_date": due_date,
        "tags": normalize_tags(payload.get("tags")),
        "created_at": now_local().isoformat(),
        "completed_at": None,
    }

    tasks = load_tasks()
    tasks.append(task)
    save_tasks(tasks)
    extra = []
    if task["due_date"]:
        extra.append(f"frist: {task['due_date']}")
    if task["tags"]:
        extra.append(f"tags: {', '.join(task['tags'])}")
    suffix = f" ({' | '.join(extra)})" if extra else ""
    return f"✅ Opgave tilføjet: _{task['text']}_ [{task['priority']}]{suffix}"


def is_overdue(task: dict) -> bool:
    if task.get("done") or not task.get("due_date"):
        return False
    today = now_local().strftime("%Y-%m-%d")
    return task["due_date"] < today


def list_tasks(
    status: str = "active",
    priority: str | None = None,
    due_filter: str | None = None,
    tag: str | None = None,
    limit: int | None = None,
) -> str:
    tasks = load_tasks()

    if status == "active":
        tasks = [t for t in tasks if not t["done"]]
    elif status == "done":
        tasks = [t for t in tasks if t["done"]]

    if priority and priority in VALID_PRIORITIES:
        tasks = [t for t in tasks if t.get("priority") == priority]

    if due_filter == "overdue":
        tasks = [t for t in tasks if is_overdue(t)]
    elif due_filter == "today":
        today = now_local().strftime("%Y-%m-%d")
        tasks = [t for t in tasks if t.get("due_date") == today]

    if tag:
        t = tag.strip().lower()
        tasks = [x for x in tasks if t in x.get("tags", [])]

    if not tasks:
        return "Du har ingen opgaver endnu."

    tasks.sort(key=lambda x: (x["done"], x.get("due_date") or "9999-12-31", x.get("priority") != "high"))
    if limit:
        tasks = tasks[:limit]

    lines = []
    for i, t in enumerate(tasks, 1):
        lines.append(format_task_line(i, t))
    return "\n".join(lines)


def complete_task(text: str) -> str:
    tasks = load_tasks()
    search = text.strip().lower()
    for t in tasks:
        if t["text"].lower() == search:
            t["done"] = True
            t["completed_at"] = now_local().isoformat()
            save_tasks(tasks)
            return f"☑️ Opgave markeret som færdig: _{t['text']}_"
    # Prøv delvis match
    for t in tasks:
        if search in t["text"].lower():
            t["done"] = True
            t["completed_at"] = now_local().isoformat()
            save_tasks(tasks)
            return f"☑️ Opgave markeret som færdig: _{t['text']}_"
    return f"Fandt ingen opgave der matcher '{text}'."


def get_tasks_overview() -> str:
    tasks = load_tasks()
    active = [t for t in tasks if not t["done"]]
    done = [t for t in tasks if t["done"]]
    overdue = [t for t in active if is_overdue(t)]
    high = [t for t in active if t.get("priority") == "high"]
    return (
        "📊 *Task-overblik*\n"
        f"• Aktive: *{len(active)}*\n"
        f"• Færdige: *{len(done)}*\n"
        f"• Overdue: *{len(overdue)}*\n"
        f"• Høj prioritet: *{len(high)}*"
    )


def parse_task_filters(payload: dict | None) -> tuple[dict, str | None]:
    if not payload:
        return {}, None

    status = str(payload.get("status", "active")).strip().lower()
    if status not in {"active", "done", "all"}:
        return {}, "⚠️ Ugyldigt status-filter. Brug active, done eller all."

    priority = str(payload.get("priority", "")).strip().lower() or None
    if priority and priority not in VALID_PRIORITIES:
        return {}, "⚠️ Ugyldig prioritet. Brug high, medium eller low."

    due_filter = str(payload.get("due", "")).strip().lower() or None
    if due_filter and due_filter not in {"overdue", "today"}:
        return {}, "⚠️ Ugyldig due-filter. Brug overdue eller today."

    tag = str(payload.get("tag", "")).strip().lower() or None
    return {
        "status": status,
        "priority": priority,
        "due_filter": due_filter,
        "tag": tag,
    }, None


def build_daily_briefing() -> str:
    try:
        events = get_todays_events()
    except Exception as e:
        logger.error(f"Briefing fejl ved events: {e}")
        events = "Kunne ikke hente kalenderen lige nu."

    overview = get_tasks_overview()
    top_tasks = list_tasks(status="active", limit=3)
    return (
        "🌅 *Godmorgen - dit overblik kl. 08:15*\n\n"
        f"{overview}\n\n"
        "🗓️ *I dag:*\n"
        f"{events}\n\n"
        "🎯 *Top 3 opgaver:*\n"
        f"{top_tasks}"
    )


def normalize_inventory_payload(payload: dict | None) -> tuple[dict | None, str | None]:
    """Validerer og normaliserer inventory payload fra Claude."""
    if not payload or not isinstance(payload, dict):
        return None, "⚠️ Jeg mangler data for at oprette varen i lageret."

    name = str(payload.get("name", "")).strip()
    if not name:
        return None, "Hvad skal varen hedde?"

    try:
        quantity = int(payload.get("quantity"))
    except (TypeError, ValueError):
        return None, "Hvor mange stk skal oprettes? (heltal)"
    if quantity <= 0:
        return None, "Antal skal være større end 0."

    try:
        price = float(payload.get("price"))
    except (TypeError, ValueError):
        return None, "Hvad er prisen i DKK?"
    if price < 0:
        return None, "Prisen kan ikke være negativ."

    category = str(payload.get("category", "")).strip()
    note = str(payload.get("note", "")).strip()

    return {
        "name": name,
        "quantity": quantity,
        "price": price,
        "category": category,
        "note": note,
    }, None


def create_inventory_item(item: dict) -> str:
    """Opretter en vare i ekstern lager-API (One view)."""
    if not INVENTORY_API_URL:
        return "⚠️ Lager-API er ikke konfigureret endnu (INVENTORY_API_URL mangler)."

    body = json.dumps(item).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
    }
    if INVENTORY_API_TOKEN:
        headers["Authorization"] = f"Bearer {INVENTORY_API_TOKEN}"

    req = urlrequest.Request(INVENTORY_API_URL, data=body, headers=headers, method="POST")

    try:
        with urlrequest.urlopen(req, timeout=INVENTORY_API_TIMEOUT_SECONDS) as resp:
            raw = resp.read().decode("utf-8")
            response_json = json.loads(raw) if raw else {}
    except urlerror.HTTPError as e:
        detail = e.read().decode("utf-8", errors="ignore")
        logger.error(f"Lager-API HTTP fejl {e.code}: {detail}")
        return "⚠️ Lager-systemet svarede med en fejl. Prøv igen."
    except urlerror.URLError as e:
        logger.error(f"Lager-API netværksfejl: {e}")
        return "⚠️ Kunne ikke kontakte lager-systemet lige nu."
    except Exception as e:
        logger.error(f"Uventet lager-fejl: {e}")
        return "⚠️ Noget gik galt ved oprettelse i lageret."

    created_name = response_json.get("name", item["name"])
    created_quantity = response_json.get("quantity", item["quantity"])
    created_price = response_json.get("price", item["price"])
    return (
        "✅ Vare oprettet i lageret:\n"
        f"• Navn: *{created_name}*\n"
        f"• Antal: *{created_quantity}*\n"
        f"• Pris: *{created_price} DKK*"
    )


# ── Claude AI ─────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = f"""Du er en personlig kalenderassistent der hjælper med aftaler og opgaver.

I dag er det: {datetime.now().strftime("%A %d. %B %Y, kl. %H:%M")} (dansk tid, Copenhagen).

Du har følgende handlinger til rådighed. Brug tags præcist som vist:

1. DAGSOVERBLIK — brugeren spørger hvad der sker i dag:
<GET_TODAY>
</GET_TODAY>

2. UGEOVERBLIK — brugeren spørger om ugen, "hvad sker der denne uge", "vis min uge":
<GET_WEEK>
</GET_WEEK>

3. OPRET AFTALE — brugeren vil booke noget. Identificer titel, dato, tid og varighed. Spørg efter manglende info. Returner når klar:
<CREATE_EVENT>
{{
  "title": "Møde med Peter",
  "start": "2026-04-16T10:00:00",
  "end": "2026-04-16T11:00:00",
  "description": "Valgfri beskrivelse"
}}
</CREATE_EVENT>

4. SLET AFTALE — brugeren vil slette en aftale. Udtræk søgeord fra aftalens navn:
<DELETE_EVENT>
{{
  "search": "tandlæge"
}}
</DELETE_EVENT>

5. VIS OPGAVER — brugeren vil se sin opgaveliste:
<GET_TASKS>
</GET_TASKS>

5b. VIS OPGAVER MED FILTER:
<GET_TASKS_FILTER>
{{
    "status": "active",
    "priority": "high",
    "due": "overdue",
    "tag": "work"
}}
</GET_TASKS_FILTER>

5c. VIS TASK-OVERBLIK:
<GET_TASK_OVERVIEW>
</GET_TASK_OVERVIEW>

5d. VIS KUN OVERDUE OPGAVER:
<GET_OVERDUE_TASKS>
</GET_OVERDUE_TASKS>

6. TILFØJ OPGAVE — brugeren vil tilføje en opgave:
<ADD_TASK>
{{
    "text": "Ring til banken",
    "priority": "high",
    "due_date": "2026-04-20",
    "tags": ["work", "økonomi"]
}}
</ADD_TASK>

7. FULDFØR OPGAVE — brugeren markerer en opgave som færdig:
<COMPLETE_TASK>
{{
  "text": "Ring til banken"
}}
</COMPLETE_TASK>

8. TILFØJ TIL LAGER — brugeren vil oprette en vare i webappens lager:
<ADD_INVENTORY>
{{
    "name": "Nike Air Max",
    "quantity": 10,
    "price": 699,
    "category": "Sko",
    "note": "Valgfri note"
}}
</ADD_INVENTORY>

Hvis der mangler data til ADD_INVENTORY (fx navn, antal eller pris), så spørg venligt om præcis én manglende ting i almindelig tekst og brug IKKE tag endnu.
Hvis der mangler data til ADD_TASK, så spørg også om præcis én manglende ting i almindelig tekst og brug IKKE tag endnu.

Svar altid på dansk. Vær venlig og kortfattet. Brug kun ét tag per svar.
"""


async def process_with_claude(user_id: int, user_message: str):
    """Sender brugerens besked til Claude og returnerer parsed handlinger."""
    if user_id not in user_sessions:
        user_sessions[user_id] = []

    user_sessions[user_id].append({"role": "user", "content": user_message})

    # Begræns historik til de seneste 20 beskeder
    history = user_sessions[user_id][-20:]

    response = anthropic_client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1000,
        system=SYSTEM_PROMPT,
        messages=history,
    )

    reply_text = response.content[0].text
    user_sessions[user_id].append({"role": "assistant", "content": reply_text})

    # Parse alle tags
    actions = {
        "get_today":     "<GET_TODAY>" in reply_text,
        "get_week":      "<GET_WEEK>" in reply_text,
        "create_event":  None,
        "delete_event":  None,
        "get_tasks":     "<GET_TASKS>" in reply_text,
        "get_tasks_filter": None,
        "get_task_overview": "<GET_TASK_OVERVIEW>" in reply_text,
        "get_overdue_tasks": "<GET_OVERDUE_TASKS>" in reply_text,
        "add_task":      None,
        "complete_task": None,
        "add_inventory": None,
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

    actions["create_event"]  = extract_json("CREATE_EVENT")
    actions["delete_event"]  = extract_json("DELETE_EVENT")
    actions["get_tasks_filter"] = extract_json("GET_TASKS_FILTER")
    actions["add_task"]      = extract_json("ADD_TASK")
    actions["complete_task"] = extract_json("COMPLETE_TASK")
    actions["add_inventory"] = extract_json("ADD_INVENTORY")

    # Rens reply_text for alle tags og JSON-blokke
    for tag in [
        "GET_TODAY",
        "GET_WEEK",
        "GET_TASKS",
        "GET_TASKS_FILTER",
        "GET_TASK_OVERVIEW",
        "GET_OVERDUE_TASKS",
        "CREATE_EVENT",
        "DELETE_EVENT",
        "ADD_TASK",
        "COMPLETE_TASK",
        "ADD_INVENTORY",
    ]:
        open_t = f"<{tag}>"
        close_t = f"</{tag}>"
        if open_t in reply_text and close_t in reply_text:
            start_idx = reply_text.find(open_t)
            end_idx   = reply_text.find(close_t) + len(close_t)
            reply_text = (reply_text[:start_idx] + reply_text[end_idx:]).strip()
        elif open_t in reply_text:
            reply_text = reply_text.replace(open_t, "").strip()

    return reply_text, actions


# ── Telegram handlers ──────────────────────────────────────────────────────────

def is_authorized(user_id: int) -> bool:
    return user_id == AUTHORIZED_USER_ID


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text("Adgang nægtet.")
        return
    await update.message.reply_text(
        "Hej! Jeg er din kalenderassistent 📅\n\n"
        "Du kan bl.a. sige:\n"
        "• _Hvad sker der i dag?_\n"
        "• _Hvordan ser min uge ud?_\n"
        "• _Book møde med Mette i morgen kl 10 i en time_\n"
        "• _Slet min tandlægeaftale_\n"
        "• _Tilføj opgave: Ring til banken_\n"
        "• _Tilføj opgave: Ring til banken, høj prioritet, frist 2026-04-20_\n"
        "• _Vis mine overdue opgaver_\n"
        "• _Giv mig task-overblik_\n"
        "• _Tilføj 10 stk Nike Air Max til lager for 699 kr_\n"
        "• _Vis mine opgaver_\n"
        "• _Færdig: Ring til banken_\n\n"
        "Skriv /briefing for dagsbriefing eller /nulstil for ny samtale.",
        parse_mode="Markdown"
    )


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        return
    user_id = update.effective_user.id
    user_sessions.pop(user_id, None)
    await update.message.reply_text("Samtalen er nulstillet. Hvad vil du?")


async def briefing_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text("Adgang nægtet.")
        return
    await update.message.reply_text(build_daily_briefing(), parse_mode="Markdown")


async def send_scheduled_briefing(context: ContextTypes.DEFAULT_TYPE):
    try:
        await context.bot.send_message(
            chat_id=AUTHORIZED_USER_ID,
            text=build_daily_briefing(),
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )
    except Exception as e:
        logger.error(f"Fejl ved planlagt briefing: {e}")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text("Adgang nægtet.")
        return

    user_id = update.effective_user.id
    user_text = update.message.text

    # Vis "skriver..." mens vi behandler
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    try:
        reply, actions = await process_with_claude(user_id, user_text)

        # ── GET_TODAY ──
        if actions["get_today"]:
            try:
                events_text = get_todays_events()
                reply = f"📅 *Dine aftaler i dag:*\n\n{events_text}"
            except Exception as e:
                logger.error(f"Fejl ved hentning af aftaler: {e}")
                reply = "⚠️ Kunne ikke hente dine aftaler. Prøv igen."

        # ── GET_WEEK ──
        elif actions["get_week"]:
            try:
                week_text = get_week_events()
                reply = f"📅 *Din uge (de næste 7 dage):*\n\n{week_text}"
            except Exception as e:
                logger.error(f"Fejl ved hentning af ugeoversigt: {e}")
                reply = "⚠️ Kunne ikke hente ugeoversigten. Prøv igen."

        # ── CREATE_EVENT ──
        elif actions["create_event"]:
            event_data = actions["create_event"]
            try:
                link = create_calendar_event(
                    title=event_data["title"],
                    start_iso=event_data["start"],
                    end_iso=event_data["end"],
                    description=event_data.get("description", ""),
                )
                reply = f"✅ *{event_data['title']}* er oprettet i kalenderen!\n[Åbn i Google Calendar]({link})"
                user_sessions.pop(user_id, None)
            except Exception as e:
                logger.error(f"Fejl ved oprettelse af kalenderaftale: {e}")
                reply = "⚠️ Kunne ikke oprette aftalen i kalenderen. Prøv igen."

        # ── DELETE_EVENT ──
        elif actions["delete_event"]:
            search = actions["delete_event"].get("search", "")
            try:
                result_msg = delete_calendar_event(search)
                reply = result_msg
            except Exception as e:
                logger.error(f"Fejl ved sletning af aftale: {e}")
                reply = "⚠️ Kunne ikke slette aftalen. Prøv igen."

        # ── GET_TASKS ──
        elif actions["get_tasks"]:
            reply = f"📋 *Dine opgaver:*\n\n{list_tasks(status='active')}"

        # ── GET_TASKS_FILTER ──
        elif actions["get_tasks_filter"] is not None:
            filters_map, filter_error = parse_task_filters(actions["get_tasks_filter"])
            if filter_error:
                reply = filter_error
            else:
                reply = f"📋 *Filtrerede opgaver:*\n\n{list_tasks(**filters_map)}"

        # ── GET_TASK_OVERVIEW ──
        elif actions["get_task_overview"]:
            reply = get_tasks_overview()

        # ── GET_OVERDUE_TASKS ──
        elif actions["get_overdue_tasks"]:
            overdue_text = list_tasks(status="active", due_filter="overdue")
            reply = f"⏰ *Overdue opgaver:*\n\n{overdue_text}"

        # ── ADD_TASK ──
        elif actions["add_task"]:
            task_payload = actions["add_task"] or {}
            if isinstance(task_payload, dict):
                reply = add_task(task_payload)
            else:
                reply = "⚠️ Kunne ikke tilføje opgaven. Mangler data."

        # ── COMPLETE_TASK ──
        elif actions["complete_task"]:
            task_text = actions["complete_task"].get("text", "")
            if task_text:
                reply = complete_task(task_text)
            else:
                reply = "⚠️ Kunne ikke markere opgaven. Mangler tekst."

        # ── ADD_INVENTORY ──
        elif actions["add_inventory"]:
            payload, validation_error = normalize_inventory_payload(actions["add_inventory"])
            if validation_error:
                reply = validation_error
            else:
                reply = create_inventory_item(payload)

        await update.message.reply_text(reply, parse_mode="Markdown", disable_web_page_preview=True)

    except Exception as e:
        logger.error(f"Fejl: {e}")
        await update.message.reply_text("Noget gik galt. Prøv igen eller skriv /nulstil.")


# ── Start botten ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Kør Google auth første gang (åbner browser)
    get_calendar_service()
    print("Google Calendar forbundet ✓")

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("nulstil", reset))
    app.add_handler(CommandHandler("briefing", briefing_now))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    if ENABLE_DAILY_BRIEFING and app.job_queue:
        try:
            hh, mm = DAILY_BRIEFING_TIME.split(":", 1)
            run_time = time(hour=int(hh), minute=int(mm), tzinfo=COPENHAGEN_TZ)
            app.job_queue.run_daily(send_scheduled_briefing, time=run_time)
            print(f"Daglig briefing aktiveret kl. {DAILY_BRIEFING_TIME}")
        except Exception as e:
            logger.error(f"Kunne ikke aktivere daglig briefing: {e}")

    print("Botten kører... Tryk Ctrl+C for at stoppe.")
    app.run_polling()