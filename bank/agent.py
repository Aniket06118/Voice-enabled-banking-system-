"""
The voice/chat banking agent (your friend's LangChain agent), now backed by the
database instead of JSON files.

Key safety rules:
  * The agent's tools are bound to the LOGGED-IN account, so it can never touch
    anyone else's money.
  * Payments of Rs 500+ need the 4-digit PIN. The PIN is typed on screen and
    sent straight to Django. It NEVER passes through the AI model.
  * The AI can only confirm payments under Rs 500 (voice confirmation).
"""
import functools
import json
import logging
import os
from typing import Optional

from asgiref.sync import async_to_sync, sync_to_async
from langchain.agents import create_agent
from langchain_core.tools import tool

from django.utils import timezone

from . import services
from .analysis import build_analysis, normalize_category, save_request
from .categories import CATEGORIES
from .models import Analysis, Payment

log = logging.getLogger(__name__)

FALLBACK_REPLY = ("Sorry, I'm having trouble right now. "
                  "Your money is safe. Please try again in a moment.")

LANG_MAP = {
    "en-IN": "English",
    "hi-IN": "Hindi",
    "ta-IN": "Tamil",
    "te-IN": "Telugu",
    "bn-IN": "Bengali",
    "mr-IN": "Marathi",
    "gu-IN": "Gujarati",
    "kn-IN": "Kannada",
    "ml-IN": "Malayalam",
    "pa-IN": "Punjabi",
    "od-IN": "Odia",
}


def build_system_prompt(account, lang: str = "en-IN") -> str:
    contacts = [f"{c.name} ({c.upi_id})" for c in account.contacts.all()]
    contacts_str = ", ".join(contacts) if contacts else "None"
    lang_name = LANG_MAP.get(lang, "Hindi" if lang.startswith("hi") else "English")

    return f"""You are EchoMint, a friendly voice banking assistant. Replies are spoken aloud, so keep them to ONE or TWO short sentences maximum. NEVER use markdown, bullet lists, asterisks, or symbols in your replies.

USER ACCOUNT CONTEXT:
- Account Holder: {account.full_name}
- Saved Contacts: {contacts_str}
- SELECTED LANGUAGE: {lang_name} ({lang})

RULES:
1. STRICT LANGUAGE ENFORCEMENT (MANDATORY):
   - The user has chosen: {lang_name}.
   - You MUST reply EXCLUSIVELY in {lang_name}.
   - NEVER switch to another language under ANY circumstance! For example, if {lang_name} is Hindi, NEVER reply in Marathi, Gujarati, English, or Tamil!
   - Even if the user transcript has misrecognized words or shared Devanagari vocabulary, your response MUST be 100% {lang_name}.
   - Always write numbers and amounts as digits (e.g. 500, 1000), not words.
2. SAVED CONTACTS RULE (CRITICAL): The ONLY saved contacts that exist for this user are: {contacts_str}. NEVER invent or hallucinate contacts (e.g. NEVER mention fake names like Priya, Vikas).
3. NAME TRANSLITERATION RULE: The user may speak names in Indian scripts (e.g. Devanagari 'राहुल वर्मा', Tamil 'ராகுல் வர்மா', Telugu 'రాహుల్ వర్మ', or 'अमन'). When calling start_payment or checking contacts, map the name to the exact contact name from the saved contacts list above (e.g. 'Rahul Verma', 'Rahul Sharma', 'Aman', 'Mom').
4. PAYMENTS: To send money you need BOTH recipient and amount. If missing, ask briefly.
   - Under 500 rupees: Call start_payment, then ONLY after user confirms ("yes"/"haan"), call confirm_pending_payment.
   - 500 rupees or more: Call start_payment. It will prompt the user to enter their 4-digit PIN on screen. NEVER ask the user to say their PIN aloud.
   - If user says no/cancel/stop: Call cancel_pending_payment.
   - If several contacts match (e.g. Rahul Sharma vs Rahul Verma), ask which one.
5. BALANCE & HISTORY: For balance or contacts questions call get_user_details. For past payments call get_payment_history. For totals ("how much sent to Aman") call get_spending_summary. Never invent numbers.
7. Never state a payment succeeded or failed unless a tool reported it.
"""


def db_tool(fn):
    """Turn a plain function into an agent tool that runs on the REQUEST thread.

    LangChain would otherwise run tools on background threads, each opening its
    own database connection (stale-connection errors on Neon, no shared
    transaction). Running them back on the request thread keeps exactly one
    connection, the same one the rest of Django uses.
    """
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        return await sync_to_async(fn)(*args, **kwargs)
    return tool(wrapper)


def build_tools(account, ui: list = None):
    """Create the tool set for ONE account (closures capture `account`).
    `ui` collects instructions for the web page (open a page, show an analysis...)."""
    ui = ui if ui is not None else []

    @db_tool
    def get_user_details() -> str:
        """Get the user's account details: name, bank, account number, balance,
        daily limit and saved contacts. Use for any question about their own
        account, balance or contacts."""
        return json.dumps(services.get_user_details(account), indent=2)

    @db_tool
    def get_payment_history(recipient_name: Optional[str] = None,
                            days: Optional[int] = None) -> str:
        """List past payments (newest first). Optionally filter by recipient
        name and/or the last N days. Use for questions about past payments."""
        rows = services.get_history(account, limit=50, recipient=recipient_name, days=days)
        return json.dumps(rows, indent=2) if rows else "No matching payments found."

    @db_tool
    def get_spending_summary(recipient_name: Optional[str] = None,
                             days: Optional[int] = None) -> str:
        """Exact total money sent, optionally for one recipient and/or the last
        N days, with a per-recipient breakdown. Use this for any 'how much' or
        'total' question."""
        return json.dumps(services.spending_summary(
            account, recipient=recipient_name, days=days), indent=2)

    @db_tool
    def start_payment(recipient_name: Optional[str] = None,
                      amount: Optional[float] = None) -> str:
        """Start a payment to a contact. Does NOT move money yet; it asks the
        user to confirm (under 500 rupees) or enter their PIN on screen
        (500 rupees or more). Call when the user wants to pay, send or transfer."""
        result = services.initiate_payment(account, recipient_name, amount)
        if result["ok"]:
            services.supersede_older_pending(account, result["payment_id"])
        return result["message"]

    @db_tool
    def confirm_pending_payment() -> str:
        """Confirm the pending payment AFTER the user has clearly said yes.
        Works only for payments under 500 rupees; larger ones need the PIN
        typed on screen."""
        pending = services.latest_pending(account)
        if pending is None:
            return "There is no payment waiting for confirmation."
        result = services.confirm_payment(account, pending.id)  # note: no PIN
        if result["code"] == "pin_required":
            return ("This payment needs the user's 4-digit PIN, entered on the screen. "
                    "Tell them to enter it there.")
        return result["message"]

    @db_tool
    def cancel_pending_payment() -> str:
        """Cancel the pending payment when the user says no or cancel."""
        pending = services.latest_pending(account)
        if pending is None:
            return "There is no payment waiting for confirmation."
        return services.cancel_payment(account, pending.id)["message"]

    @db_tool
    def analyze_spending(category: str = "all", period: str = "last_30_days") -> str:
        """Build a spending analysis with charts. category: one of food, travel,
        shopping, bills, entertainment, health, groceries, transfers, other, or all.
        period: this_month, last_month, last_7_days, last_30_days, last_3_months,
        last_6_months, this_year or last_year."""
        params = {"category": normalize_category(category), "period": period or "last_30_days"}
        spec = build_analysis(account, params["category"], params["period"])
        saved = save_request(account, params["category"], params["period"])
        ui.append({"type": "analysis", "id": saved["id"], "url": saved["url"], "title": spec["title"]})
        if spec["empty"]:
            return spec["message"] + " The analysis page is ready anyway."
        return f"Analysis ready: {spec['title']}. The user can tap Open analysis to see the charts."

    @tool
    def navigate(page: str) -> str:
        """Open a page of the banking app: dashboard, accounts, payments,
        investments, cards, loans, support, profile, settings, analytics,
        notifications."""
        page = str(page or "").lower().strip()
        if page not in PAGES:
            return f"Unknown page. Pages: {', '.join(PAGES)}."
        ui.append({"type": "navigate", "page": page})
        return f"Opened {page}."

    @tool
    def open_form(form: str, recipient: Optional[str] = None, amount: Optional[float] = None) -> str:
        """Open a form so the user can do it by hand: send (send money), bills
        (pay a bill), recharge (mobile recharge) or gold (buy digital gold).
        Optional recipient/amount pre-fill the form. Does NOT move money."""
        form = str(form or "").lower().strip()
        if form not in FORMS:
            return f"Unknown form. Forms: {', '.join(FORMS)}."
        ui.append({"type": "form", "form": form, "recipient": recipient, "amount": amount})
        return f"Opened the {form} form."

    @tool
    def change_setting(setting: str, value: str) -> str:
        """Change an app setting. setting: language (value: a code like hi-IN,
        en-IN, ta-IN), spoken_replies (on/off), text_size (large/extra_large),
        card_frozen (on/off)."""
        setting, value = str(setting).lower().strip(), str(value).strip()
        ok = {"language": value in LANG_MAP, "spoken_replies": value.lower() in ("on", "off"),
              "text_size": value.lower() in ("large", "extra_large"), "card_frozen": value.lower() in ("on", "off")}
        if not ok.get(setting):
            return "That setting or value is not supported."
        ui.append({"type": "setting", "setting": setting, "value": value})
        return f"{setting} set to {value}."

    return [get_user_details, get_payment_history, get_spending_summary,
            start_payment, confirm_pending_payment, cancel_pending_payment,
            analyze_spending, navigate, open_form, change_setting]


def model_names() -> list:
    """Main model first, then a lighter backup that is tried if the first one fails."""
    primary = os.getenv("AGENT_MODEL", "gemini-3.1-flash-lite")
    backup = os.getenv("AGENT_FALLBACK_MODEL", "gemini-2.5-flash")
    return [primary] if backup == primary else [primary, backup]


def get_model(name=None):
    """The LLM. Generous timeout and retries so network jitter never breaks the agent."""
    from langchain_google_genai import ChatGoogleGenerativeAI
    api_key = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
    kwargs = {}
    if api_key:
        kwargs["google_api_key"] = api_key
    return ChatGoogleGenerativeAI(
        model=name or model_names()[0],
        timeout=35,
        max_retries=2,
        **kwargs,
    )


def extract_text(content) -> str:
    """LangChain content can be a string or a list of blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
        ).strip()
    return str(content)


def run_chat(account, history: list, message: str, lang: str = "en-IN") -> dict:
    """Run one conversation turn with account context and chosen language.
    If the main model fails (timeout, 504), the backup model is tried.
    Never raises: if everything fails the user gets a calm spoken fallback."""
    last_error = None
    system_prompt = build_system_prompt(account, lang=lang)
    tools = build_tools(account)

    for name in model_names():
        ui = []
        try:
            agent = create_agent(model=get_model(name), system_prompt=system_prompt, tools=tools)
            messages = list(history) + [{"role": "user", "content": message}]
            result = async_to_sync(agent.ainvoke)({"messages": messages})
            reply = extract_text(result["messages"][-1].content) or FALLBACK_REPLY
            return {"reply": reply, "error": None}
        except Exception as exc:  # noqa: BLE001
            log.exception("Agent failed with model %s", name)
            last_error = f"{type(exc).__name__}: {exc}"
    return {"reply": FALLBACK_REPLY, "error": last_error, "ui": []}


def pending_payload(account):
    """Tell the website whether it must show the PIN pad."""
    p = services.latest_pending(account)
    if p is None:
        return None
    return {"id": str(p.id), "amount": str(p.amount), "recipient": p.recipient_name,
            "needs_pin": p.auth_required == Payment.Auth.PIN}
