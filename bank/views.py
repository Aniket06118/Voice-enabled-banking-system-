"""JSON API used by the website (and anything else that wants to talk to the bank)."""
import base64
import json
import logging
import os
from functools import wraps

from django.conf import settings
from django.contrib.auth import authenticate, login, logout
from django.db import connection
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST

from . import agent, intents, services, voice
from .models import Account, VoiceProfile

log = logging.getLogger(__name__)

ERROR_STATUS = {
    "payment_not_found": 404,
    "pin_locked": 423,
    "not_logged_in": 401,
}


def _body(request) -> dict:
    try:
        data = json.loads(request.body or b"{}")
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


def _respond(result: dict) -> JsonResponse:
    status = 200 if result["ok"] else ERROR_STATUS.get(result["code"], 400)
    return JsonResponse(result, status=status)


def api_login_required(view):
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated or not hasattr(request.user, "account"):
            return _respond(services._result(False, "not_logged_in", "Please log in first."))
        return view(request, *args, **kwargs)
    return wrapper


def _voice_verified(request) -> bool:
    """Verified stays valid while the person keeps using the app (idle timeout),
    up to a hard limit. A page refresh therefore never asks again."""
    if not settings.VOICE_REQUIRED:
        return True
    now = time.time()
    at = request.session.get("voice_verified_at")
    if not at or now - at > settings.VOICE_MAX_HOURS * 3600:
        return False
    last = request.session.get("voice_last_active", at)
    if now - last > settings.VOICE_IDLE_MINUTES * 60:
        return False
    if now - last > 60:
        request.session["voice_last_active"] = now
    return True


def _mark_verified(request, method: str):
    now = time.time()
    request.session["voice_verified_at"] = now
    request.session["voice_last_active"] = now
    request.session["voice_method"] = method          # "voice" or "pin"


def voice_required(view):
    """Block the endpoint until this session has passed the voice check."""
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not _voice_verified(request):
            return JsonResponse({"ok": False, "code": "voice_required",
                                 "message": "Please verify your voice first."}, status=403)
        return view(request, *args, **kwargs)
    return wrapper


@require_GET
def health(request):
    """Open /api/health/ to check the server AND the database connection."""
    with connection.cursor() as cur:
        cur.execute("SELECT 1")
    return JsonResponse({"ok": True, "database": connection.vendor})


@require_GET
@ensure_csrf_cookie
def csrf(request):
    return JsonResponse({"ok": True})


@require_POST
def login_view(request):
    data = _body(request)
    user = authenticate(request, username=data.get("phone"), password=data.get("password"))
    if user is None or not hasattr(user, "account"):
        return _respond(services._result(False, "bad_credentials", "Wrong phone number or password."))
    login(request, user)
    return JsonResponse({"ok": True, "code": "logged_in",
                         "message": f"Welcome back, {user.account.full_name}.",
                         "user": services.get_user_details(user.account)})


@require_POST
def logout_view(request):
    logout(request)
    return JsonResponse({"ok": True})


@require_GET
@api_login_required
def me(request):
    return JsonResponse({"ok": True, **services.get_user_details(request.user.account)})


@require_GET
@api_login_required
def history(request):
    q = request.GET
    rows = services.get_history(
        request.user.account,
        limit=min(int(q.get("limit", 20) or 20), 200),
        recipient=q.get("recipient"),
        days=q.get("days"),
    )
    return JsonResponse({"ok": True, "payments": rows})


@require_GET
@api_login_required
def summary(request):
    q = request.GET
    return JsonResponse({"ok": True, **services.spending_summary(
        request.user.account, recipient=q.get("recipient"), days=q.get("days"))})


@require_POST
@api_login_required
@voice_required
def start_payment(request):
    d = _body(request)
    return _respond(services.initiate_payment(
        request.user.account, d.get("recipient"), d.get("amount"),
        idempotency_key=d.get("idempotency_key")))


@require_POST
@api_login_required
@voice_required
def confirm_payment(request, payment_id):
    d = _body(request)
    result = services.confirm_payment(request.user.account, payment_id, pin=d.get("pin"))
    if result["ok"] and result["code"] == "success":
        # Let the agent know this payment is done (the PIN went straight to Django,
        # the AI never saw it).
        _remember(request, "assistant", result["message"])
    return _respond(result)


@require_POST
@api_login_required
@voice_required
def cancel_payment(request, payment_id):
    return _respond(services.cancel_payment(request.user.account, payment_id))


@require_GET
@api_login_required
def notifications(request):
    return JsonResponse({"ok": True, "notifications": services.notifications(request.user.account)})


@require_POST
@api_login_required
@voice_required
def change_pin(request):
    d = _body(request)
    return _respond(services.change_pin(request.user.account, d.get("old_pin"), d.get("new_pin")))


@require_POST
@api_login_required
@voice_required
def daily_limit(request):
    d = _body(request)
    return _respond(services.set_daily_limit(request.user.account, d.get("daily_limit"), d.get("pin")))


# ---------------------------------------------------------------- the website

@ensure_csrf_cookie
def landing(request):
    """Public home page with the 3D "how a payment works" machine."""
    return render(request, "bank/landing.html")


@ensure_csrf_cookie
def login_page(request):
    if request.user.is_authenticated and hasattr(request.user, "account"):
        return redirect("/app/")
    return render(request, "bank/login.html")


@ensure_csrf_cookie
def index(request):
    """The banking app itself (chat, mic, balance, PIN pad). Needs a login."""
    if not (request.user.is_authenticated and hasattr(request.user, "account")):
        return redirect("/login/")
    return render(request, "bank/app.html")


MAX_HISTORY = 20


def _remember(request, role: str, content: str):
    chat = request.session.get("chat", [])
    chat.append({"role": role, "content": content})
    request.session["chat"] = chat[-MAX_HISTORY:]


@require_POST
@api_login_required
@voice_required
def agent_chat(request):
    """One turn with the banking agent (typed text or the transcript of speech)."""
    body = _body(request)
    message = str(body.get("message", "")).strip()[:500]
    lang = str(body.get("lang", "en-IN")).strip() or "en-IN"
    if not message:
        return JsonResponse({"ok": False, "code": "empty",
                             "message": "I didn't catch that."}, status=400)
    account = request.user.account

    # If the user changed language, clear older turns in the other language
    last_lang = request.session.get("chat_lang")
    if last_lang and last_lang != lang:
        request.session["chat"] = []
    request.session["chat_lang"] = lang

    out = agent.run_chat(account, request.session.get("chat", []), message, lang=lang)
    _remember(request, "user", message)
    _remember(request, "assistant", out["reply"])
    analysis = next(({"id": u["id"], "url": u["url"], "title": u["title"]} for u in ui if u.get("type") == "analysis"), None)
    payload = {"ok": True, "reply": out["reply"], "ui": ui, "analysis": analysis, "source": source,
               "ms": round((time.perf_counter() - t0) * 1000),
               "pending_payment": agent.pending_payload(account),
               "balance": str(Account.objects.get(pk=account.pk).balance)}
    if settings.DEBUG and out.get("error"):
        payload["debug_error"] = out["error"]
    return JsonResponse(payload)


@require_POST
@api_login_required
def agent_reset(request):
    request.session["chat"] = []
    return JsonResponse({"ok": True})


# ---------------------------------------------------------------- Sarvam voice

def _sarvam_client():
    """Return a SarvamAI client using the key from the environment."""
    from sarvamai import SarvamAI
    key = os.environ.get("SARVAM_API_KEY", "")
    if not key:
        raise RuntimeError("SARVAM_API_KEY environment variable is not set.")
    return SarvamAI(api_subscription_key=key)


@require_POST
@api_login_required
def voice_stt(request):
    """
    POST multipart/form-data with:
        audio   – the recorded audio file (any format Sarvam accepts: webm, wav, mp3…)
        lang    – BCP-47 code e.g. hi-IN  (optional; Sarvam auto-detects if omitted)

    Returns: {"ok": true, "transcript": "...", "language_code": "hi-IN"}

    Safety: the raw audio bytes are forwarded directly to Sarvam and discarded.
    They are NEVER written to disk or logged.
    """
    audio_file = request.FILES.get("audio")
    if audio_file is None:
        return JsonResponse({"ok": False, "code": "no_audio",
                             "message": "No audio file received."}, status=400)

    lang = request.POST.get("lang", "").strip() or None
    try:
        client = _sarvam_client()
        transcribe_args = {
            "file": (audio_file.name or "audio.webm", audio_file, audio_file.content_type),
            "model": "saaras:v4",
            "mode": "transcribe",
        }
        if lang:
            transcribe_args["language_code"] = lang
        response = client.speech_to_text.transcribe(**transcribe_args)
        return JsonResponse({
            "ok": True,
            "transcript": response.transcript or "",
            "language_code": response.language_code or lang or "en-IN",
        })
    except Exception as exc:  # noqa: BLE001
        log.exception("Sarvam STT failed")
        return JsonResponse({"ok": False, "code": "stt_error",
                             "message": str(exc)}, status=502)


@require_POST
@api_login_required
def voice_tts(request):
    """
    POST JSON: {"text": "...", "language_code": "hi-IN"}

    Returns: {"ok": true, "audio_b64": "<base64 WAV>"}

    Safety: the text sent here is the AGENT'S reply, not the user's speech.
    The PIN pad bypass in the UI ensures PIN digits are never in this text.
    Audio bytes are returned directly and never stored.
    """
    data = _body(request)
    text = str(data.get("text", "")).strip()[:1000]
    lang = str(data.get("language_code", "en-IN")).strip() or "en-IN"

    if not text:
        return JsonResponse({"ok": False, "code": "no_text",
                             "message": "No text to speak."}, status=400)

    # Map language code to a reasonable default Bulbul v4-flash speaker.
    # If the language is not in the map we fall back to the default persona.
    _SPEAKER_MAP = {
        "hi-IN": "shubh_enhi_ads",
        "en-IN": "shubh_enhi_ads",
        "ta-IN": "shubh_enhi_ads",
        "te-IN": "shubh_enhi_ads",
        "bn-IN": "shubh_enhi_ads",
        "mr-IN": "shubh_enhi_ads",
        "gu-IN": "shubh_enhi_ads",
        "kn-IN": "shubh_enhi_ads",
        "ml-IN": "shubh_enhi_ads",
        "pa-IN": "shubh_enhi_ads",
        "od-IN": "shubh_enhi_ads",
    }
    speaker = _SPEAKER_MAP.get(lang, "shubh_enhi_ads")

    try:
        client = _sarvam_client()
        response = client.text_to_speech.convert(
            text=text,
            model="bulbul:v4-flash",
            language_code=lang,
            speaker=speaker,
            speech_sample_rate=22050,
        )
        audio_b64 = "".join(response.audios)
        return JsonResponse({"ok": True, "audio_b64": audio_b64})
    except Exception as exc:  # noqa: BLE001
        log.exception("Sarvam TTS failed")
        return JsonResponse({"ok": False, "code": "tts_error",
                             "message": str(exc)}, status=502)
