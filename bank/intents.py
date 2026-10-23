"""
Instant answers for the most common requests, without calling the AI model.

Gemini needs 1-4 seconds (two round trips when it uses a tool). These requests
are answered straight from the database in a few milliseconds instead:

    "yes" / "haan"                 confirm the waiting payment (under 500 only)
    "no" / "cancel" / "नहीं"        cancel it
    "what's my balance"            balance
    "send 300 to Aman"             start a payment (same services.py rules)
    "pay my electricity bill 1200" start a bill payment
    "open settings" / "सेटिंग्स खोलो" open a page or form
    "analyse my food spending last month"  build an analysis
    "show my last payments"        recent payments

Anything else (or any language other than English/Hindi, or anything slightly
unclear) returns None and goes to the AI agent as before. The safety rules are
unchanged because every money action still goes through services.py.
"""
import re

from . import services
from .analysis import build_analysis, category_from_text, period_from_text, save_request
from .models import Payment

FAST_LANGS = {"en-IN", "hi-IN"}

DEVANAGARI_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")

YES = {"yes", "yeah", "yep", "yup", "sure", "ok", "okay", "confirm", "confirmed", "go ahead", "do it",
       "send it", "yes send", "yes please", "yes confirm", "please send", "haan", "han", "ha", "haa",
       "haanji", "haan ji", "ji", "ji haan", "theek hai", "thik hai", "kar do", "bhej do", "haan bhej do",
       "हाँ", "हां", "हा", "जी", "जी हाँ", "जी हां", "ठीक है", "भेज दो", "कर दो", "हाँ भेज दो", "हां भेज दो"}
NO = {"no", "nope", "cancel", "cancel it", "stop", "dont", "don't", "do not", "no thanks", "dont send",
      "don't send", "nahi", "nahin", "na", "mat karo", "mat bhejo", "ruko", "cancel karo", "rehne do",
      "नहीं", "नही", "ना", "रद्द करो", "मत भेजो", "रुको", "कैंसल", "कैंसल करो", "रहने दो"}

PAGE_WORDS = {
    "dashboard": ["dashboard", "home", "main page", "होम", "डैशबोर्ड", "मुख्य पेज"],
    "accounts": ["account", "accounts", "my account", "खाता", "अकाउंट"],
    "payments": ["payments", "transfers", "transactions", "statement", "statements", "passbook",
                 "पेमेंट्स", "लेनदेन", "स्टेटमेंट", "पासबुक"],
    "investments": ["investments", "investment", "invest", "sip", "निवेश", "इन्वेस्टमेंट"],
    "cards": ["card", "cards", "debit card", "कार्ड"],
    "loans": ["loan", "loans", "emi", "लोन", "कर्ज"],
    "support": ["support", "help", "faq", "मदद", "सहायता", "हेल्प"],
    "profile": ["profile", "प्रोफाइल", "प्रोफ़ाइल"],
    "settings": ["settings", "setting", "सेटिंग", "सेटिंग्स"],
    "analytics": ["analytics", "insights", "spending analysis", "एनालिटिक्स", "विश्लेषण पेज"],
    "notifications": ["notifications", "notification", "alerts", "सूचना", "सूचनाएं", "नोटिफिकेशन"],
}
PAGE_HI = {"dashboard": "डैशबोर्ड", "accounts": "खाता", "payments": "पेमेंट्स", "investments": "निवेश",
           "cards": "कार्ड", "loans": "लोन", "support": "मदद", "profile": "प्रोफाइल",
           "settings": "सेटिंग्स", "analytics": "विश्लेषण", "notifications": "सूचनाएं"}
OPEN_VERBS = ["open", "go to", "goto", "show", "take me", "switch to", "display", "navigate", "bring up",
              "khol", "kholo", "dikha", "dikhao", "le chalo", "jao", "खोल", "खोलो", "दिखा", "दिखाओ", "ले चलो", "जाओ"]
FORMS = {
    "send": ["send money", "transfer money", "money transfer", "पैसे भेजने", "पैसे भेजना"],
    "bills": ["pay bill", "pay a bill", "pay bills", "bill payment", "bill bharna", "बिल भरना", "बिल भरो"],
    "recharge": ["recharge", "रिचार्ज"],
    "gold": ["buy gold", "digital gold", "sona kharid", "सोना खरीद"],
}
PAY_VERBS = ["send", "pay", "transfer", "give", "bhej", "bhejo", "bhejna", "de do", "dedo", "chuka",
             "भेज", "भेजो", "भेजना", "दे दो", "चुका", "पे कर", "ट्रांसफर", "भुगतान"]
BILL_WORDS = {   # spoken word -> text that appears in the biller's saved name
    "electric": ["electricity", "electric", "bijli", "light bill", "बिजली"],
    "jal": ["water", "paani", "pani", "jal", "पानी", "जल"],
    "gas": ["gas", "गैस"],
    "broadband": ["broadband", "wifi", "wi-fi", "internet", "ब्रॉडबैंड", "इंटरनेट"],
    "gold": ["gold", "sona", "सोना"],
}
OPERATORS = {"jio": ["jio", "जियो"], "airtel prepaid": ["airtel", "एयरटेल"], "vi prepaid": [" vi ", "vodafone", "idea", "वोडाफोन"]}
BALANCE = ["balance", "how much money", "how much do i have", "kitna paisa", "kitne paise", "बैलेंस",
           "बकाया", "कितना पैसा", "कितने पैसे", "शेष राशि"]
HISTORY = ["last payments", "recent payments", "last transactions", "recent transactions", "payment history",
           "transaction history", "who did i pay", "पिछले लेनदेन", "पिछले पेमेंट", "हाल के पेमेंट"]
ANALYSE = ["analyse", "analyze", "analysis", "breakdown", "break down", "spending report", "chart",
           "विश्लेषण", "खर्च का हिसाब", "खर्चे का हिसाब", "hisaab", "hisab"]
GREET = {"hi", "hello", "hey", "namaste", "namaskar", "good morning", "good evening", "नमस्ते", "नमस्कार"}

T = {
    "balance": {"en": "Your balance is {bal}.", "hi": "आपका बैलेंस {bal} है।"},
    "nav": {"en": "Opening {page}.", "hi": "{page} खोल रहा हूँ।"},
    "form": {"en": "Here is the {form} form.", "hi": "{form} का फॉर्म खोल दिया है।"},
    "ask_yes": {"en": "Send {amt} to {name}? Say yes to confirm.", "hi": "{name} को {amt} भेजूँ? पुष्टि के लिए हाँ कहें।"},
    "ask_pin": {"en": "To send {amt} to {name}, please type your 4-digit PIN on the screen.",
                "hi": "{name} को {amt} भेजने के लिए स्क्रीन पर अपना 4 अंकों का PIN डालें।"},
    "sent": {"en": "Done. {amt} sent to {name}. Your balance is now {bal}.",
             "hi": "हो गया। {name} को {amt} भेज दिए गए। अब आपका बैलेंस {bal} है।"},
    "cancelled": {"en": "Okay, I've cancelled that payment.", "hi": "ठीक है, वह पेमेंट रद्द कर दिया।"},
    "nothing": {"en": "There's no payment waiting for your answer.", "hi": "अभी कोई पेमेंट आपके जवाब का इंतज़ार नहीं कर रहा।"},
    "needs_pin": {"en": "This one needs your PIN. Please type it on the screen.", "hi": "इसके लिए PIN चाहिए। कृपया स्क्रीन पर डालें।"},
    "analysis": {"en": "Your analysis is ready: {title}. Tap Open analysis, or say open it.",
                 "hi": "आपका खर्च विश्लेषण तैयार है। देखने के लिए 'विश्लेषण खोलें' दबाएँ या कहें 'खोलो'।"},
    "no_analysis": {"en": "There's no analysis to open yet. Try: analyse my spending this month.",
                    "hi": "अभी कोई विश्लेषण नहीं है। कहें: इस महीने का खर्च विश्लेषण करो।"},
    "history": {"en": "Your last payments: {items}.", "hi": "आपके पिछले पेमेंट: {items}।"},
    "no_history": {"en": "You haven't made any payments yet.", "hi": "अभी तक कोई पेमेंट नहीं हुआ है।"},
    "hello": {"en": "Hello {first}! What would you like to do?", "hi": "नमस्ते {first}! बताइए, क्या करना है?"},
    "setting": {"en": "Done.", "hi": "हो गया।"},
}
FORM_NAMES = {"send": ("send money", "पैसे भेजने"), "bills": ("bill payment", "बिल भुगतान"),
              "recharge": ("recharge", "रिचार्ज"), "gold": ("digital gold", "डिजिटल गोल्ड")}
LANG_WORDS = {"hi-IN": ["hindi", "हिंदी", "हिन्दी"], "en-IN": ["english", "अंग्रेज़ी", "अंग्रेजी", "इंग्लिश"],
              "ta-IN": ["tamil"], "te-IN": ["telugu"], "bn-IN": ["bengali", "bangla"], "mr-IN": ["marathi"],
              "gu-IN": ["gujarati"], "kn-IN": ["kannada"], "ml-IN": ["malayalam"], "pa-IN": ["punjabi"],
              "od-IN": ["odia", "oriya"]}


def _clean(text: str) -> str:
    t = str(text or "").translate(DEVANAGARI_DIGITS).lower()
    t = re.sub(r"[?!.,।\"'“”‘’]", " ", t)
    return " ".join(t.split())


def _has(t: str, words) -> bool:
    padded = f" {t} "
    return any((f" {w.strip()} " in padded) if len(w.strip()) <= 3 else (w in t) for w in words)


def _amount(t: str):
    m = re.search(r"(?:₹|rs\.?|inr)?\s*(\d[\d,]*(?:\.\d{1,2})?)\s*(?:rupees?|rs|रुपये|रुपए|रुपया)?", t)
    if not m:
        return None
    try:
        v = float(m.group(1).replace(",", ""))
        return v if v > 0 else None
    except ValueError:
        return None


def _say(key, lang, **kw):
    return T[key]["hi" if lang == "hi-IN" else "en"].format(**kw)


def _recipient(account, t: str):
    """The contact (or ambiguous first-name token) mentioned in the sentence."""
    contacts = list(account.contacts.all())
    norm = services._normalize_name(t)
    for c in sorted(contacts, key=lambda c: -len(c.name)):            # full names first
        if c.name.lower() in t or c.name.lower() in norm:
            return c.name
    for key, words in BILL_WORDS.items():                              # "electricity bill" -> biller
        if _has(t, words):
            hit = [c for c in contacts if key in c.name.lower()]
            if len(hit) == 1:
                return hit[0].name
    if "recharge" in t or "रिचार्ज" in t:
        for key, words in OPERATORS.items():
            if _has(f" {t} ", words):
                hit = [c for c in contacts if key in c.name.lower()]
                if len(hit) == 1:
                    return hit[0].name
    tokens = set(norm.split()) | set(t.split())
    for c in contacts:                                                 # first-name only ("Rahul")
        for w in c.name.lower().split():
            if len(w) >= 3 and w in tokens:
                return w                                               # services will ask "which Rahul?"
    return None


def handle(account, text: str, lang: str, session) -> dict:
    """Return {"reply", "ui"} for an instant answer, or None to use the AI agent."""
    if lang not in FAST_LANGS:
        return None
    t = _clean(text)
    if not t or len(t) > 160:
        return None
    first = account.full_name.split()[0]
    pending = services.latest_pending(account)

    # 1. yes / no to a waiting payment (whole utterance must be a yes or a no)
    if t in YES or t in NO:
        if pending is None:
            return {"reply": _say("nothing", lang), "ui": []}
        if t in NO:
            services.cancel_payment(account, pending.id)
            return {"reply": _say("cancelled", lang), "ui": []}
        if pending.auth_required == Payment.Auth.PIN:
            return {"reply": _say("needs_pin", lang), "ui": []}
        r = services.confirm_payment(account, pending.id)
        if not r["ok"]:
            return {"reply": r["message"], "ui": []} if lang == "en-IN" else None
        account.refresh_from_db()
        return {"reply": _say("sent", lang, amt=services.fmt(pending.amount), name=pending.recipient_name,
                              bal=services.fmt(account.balance)), "ui": [{"type": "refresh"}]}

    if t in GREET:
        return {"reply": _say("hello", lang, first=first), "ui": []}

    # 2. language switch ("speak in hindi", "हिंदी में बोलो")
    if _has(t, ["speak in", "talk in", "switch to", "change language", "language", "mein bolo", "में बोलो", "में बात"]):
        for code, words in LANG_WORDS.items():
            if _has(t, words):
                return {"reply": _say("setting", code if code in FAST_LANGS else "en-IN"),
                        "ui": [{"type": "setting", "setting": "language", "value": code}]}

    # 3. analysis ("analyse my food spending last month")
    if _has(t, ANALYSE):
        cat, period = category_from_text(t), period_from_text(t)
        spec = build_analysis(account, cat, period)
        saved = save_request(account, cat, period)
        session["last_analysis"] = saved["url"]
        return {"reply": _say("analysis", lang, title=spec["title"]),
                "ui": [{"type": "analysis", "id": saved["id"], "url": saved["url"], "title": spec["title"]}]}
    if t in ("open it", "open analysis", "open the analysis", "show it", "खोलो", "इसे खोलो", "दिखाओ", "kholo"):
        last = session.get("last_analysis")
        if not last:
            return {"reply": _say("no_analysis", lang), "ui": []}
        url = last if str(last).startswith("/") else f"/analysis/{last}/"
        return {"reply": _say("nav", lang, page="analysis" if lang == "en-IN" else "विश्लेषण"),
                "ui": [{"type": "open_analysis", "url": url}]}

    # 4. payment with recipient AND amount
    if _has(t, PAY_VERBS) or ("recharge" in t and _amount(t)):
        amount, who = _amount(t), _recipient(account, t)
        if amount and who:
            r = services.initiate_payment(account, who, amount)
            if not r["ok"]:
                return {"reply": r["message"], "ui": []} if lang == "en-IN" else None
            services.supersede_older_pending(account, r["payment_id"])
            key = "ask_pin" if r["code"] == "needs_pin" else "ask_yes"
            return {"reply": _say(key, lang, amt=services.fmt(r["amount"]), name=r["recipient"]), "ui": []}
        # missing a piece: the AI agent asks the follow-up question
        if not any(_has(t, w) for w in FORMS.values()):
            return None

    # 5. balance
    if _has(t, BALANCE) and not _has(t, ["spent", "spend", "sent", "history", "खर्च"]):
        account.refresh_from_db()
        return {"reply": _say("balance", lang, bal=services.fmt(account.balance)), "ui": []}

    # 6. recent payments
    if _has(t, HISTORY):
        rows = services.get_history(account, limit=3)
        if not rows:
            return {"reply": _say("no_history", lang), "ui": [{"type": "navigate", "page": "payments"}]}
        sep = ", " if lang == "en-IN" else ", "
        items = sep.join(f"{services.fmt(r['amount'])} {'to' if lang == 'en-IN' else '→'} {r['recipient']}" for r in rows)
        return {"reply": _say("history", lang, items=items), "ui": [{"type": "navigate", "page": "payments"}]}

    # 7. forms ("I want to pay a bill", "recharge karna hai") - only plain requests;
    #    "send money to my friend from college" has a target, so the AI handles it
    targeted = re.search(r"(money|paise|पैसे)\s+(to|ko|को)\s+\S", t) or len(t.split()) > 8
    for form, words in FORMS.items():
        if not targeted and _has(t, words):
            en, hi = FORM_NAMES[form]
            return {"reply": _say("form", lang, form=en if lang == "en-IN" else hi),
                    "ui": [{"type": "form", "form": form}]}

    # 8. simple app actions
    if _has(t, ["log out", "logout", "sign out", "लॉग आउट"]):
        return {"reply": "Logging you out." if lang == "en-IN" else "लॉग आउट कर रहा हूँ।", "ui": [{"type": "logout"}]}
    if _has(t, ["download statement", "download my statement", "स्टेटमेंट डाउनलोड"]):
        return {"reply": _say("setting", lang), "ui": [{"type": "download_statement"}]}
    if _has(t, ["unfreeze", "unblock my card", "card chalu", "कार्ड चालू"]):
        return {"reply": _say("setting", lang), "ui": [{"type": "setting", "setting": "card_frozen", "value": "off"}]}
    if _has(t, ["freeze my card", "freeze card", "block my card", "card band", "कार्ड बंद"]):
        return {"reply": _say("setting", lang), "ui": [{"type": "setting", "setting": "card_frozen", "value": "on"}]}
    if _has(t, ["bigger text", "larger text", "text bigger", "bada text", "बड़ा"]):
        return {"reply": _say("setting", lang), "ui": [{"type": "setting", "setting": "text_size", "value": "extra_large"}]}
    if _has(t, ["smaller text", "normal text", "छोटा"]):
        return {"reply": _say("setting", lang), "ui": [{"type": "setting", "setting": "text_size", "value": "large"}]}

    # 9. open a page ("open settings", "go to my profile", "सेटिंग्स खोलो", or just "settings")
    verb = _has(t, OPEN_VERBS)
    for page, words in PAGE_WORDS.items():
        if _has(t, words) and (verb or t in words or len(t.split()) <= 2):
            name = page if lang == "en-IN" else PAGE_HI[page]
            return {"reply": _say("nav", lang, page=name), "ui": [{"type": "navigate", "page": page}]}
    return None
