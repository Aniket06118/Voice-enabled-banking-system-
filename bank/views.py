"""JSON API used by the website (and anything else that wants to talk to the bank)."""
import json
import secrets
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
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
    user = authenticate(request, username=services.normalize_phone(data.get("phone")),
                        password=data.get("password"))
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
        idempotency_key=d.get("idempotency_key"), reference=d.get("reference", "")))


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
    return render(request, "bank/login.html",
                  {"demo_phone": settings.DEMO_PHONE, "demo_password": settings.DEMO_PASSWORD})


@ensure_csrf_cookie
def forgot_page(request):
    return render(request, "bank/forgot.html")


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
    """One turn with the banking agent (typed text or the transcript of speech).
    Common requests are answered instantly (bank/intents.py); everything else
    goes to the AI agent. Both paths use the same services.py money rules."""
    t0 = time.perf_counter()
    body = _body(request)
    message = str(body.get("message", "")).strip()[:500]
    lang = str(body.get("lang", "en-IN")).strip() or "en-IN"
    if lang not in agent.LANG_MAP:
        lang = "en-IN"
    if not message:
        return JsonResponse({"ok": False, "code": "empty",
                             "message": "I didn't catch that."}, status=400)
    account = request.user.account

    # A new language starts a fresh conversation (older turns would pull the model back).
    if request.session.get("chat_lang") not in (None, lang):
        request.session["chat"] = []
    request.session["chat_lang"] = lang

    out = intents.handle(account, message, lang, request.session)
    source = "instant"
    if out is None:
        out = agent.run_chat(account, request.session.get("chat", []), message, lang=lang)
        source = "ai"
    ui = out.get("ui") or []
    for item in ui:
        if item.get("type") == "analysis":
            request.session["last_analysis"] = item["url"]
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


# ------------------------------------------------------------ voice security

VOICE_LOCK_AFTER = 5            # failed attempts before a short pause
VOICE_LOCK_MINUTES = 2
CHALLENGE_SECONDS = 15          # the digits on screen change every 15 s: a mimic has 15 s, no more
CHALLENGE_GRACE = 6             # upload + checking time for someone who started speaking in time
CHALLENGE_DIGITS = 8            # read aloud when logging in. Measured: 8 digits (~4 s of speech) widens the
                                # gap between owner and impostor in a noisy room from +0.06 to +0.20
ENROLL_DIGITS = 8               # longer clips when teaching the voice: much better accuracy
ENROLL_SAMPLES = 3
MIN_CONSISTENCY = 0.40          # the 3 enrollment clips must sound like one person
CONFIDENT_MARGIN = 0.10         # this far above the line counts as "clearly the owner"
MAX_AUDIO_BYTES = 1_500_000

_pool = ThreadPoolExecutor(max_workers=4)   # lets the speaker check and the digit check run together

FAIL_MESSAGES = {
    "voice_mismatch": "That didn't sound like you. Read the new number in your normal voice, close to the mic.",
    "phrase_mismatch": "I heard your voice, but not those digits. Please read the new number.",
    "no_digits": "I couldn't hear the numbers. Please read them a little louder.",
}


def _voice_problem(exc: voice.VoiceError) -> JsonResponse:
    status = 503 if exc.code in ("engine_missing", "stt_unavailable") else 400
    return JsonResponse({"ok": False, "code": exc.code, "message": exc.message,
                         "fallback_pin": exc.code in ("engine_missing", "stt_unavailable")},
                        status=status)


def _read_audio(request, expected: int):
    files = request.FILES.getlist("audio")
    if len(files) != expected:
        raise voice.VoiceError("audio_invalid", f"Expected {expected} recording(s).")
    out = []
    for f in files:
        if f.size > MAX_AUDIO_BYTES:
            raise voice.VoiceError("audio_too_long", "That recording was too long.")
        out.append(f.read())
    return out


@require_GET
@api_login_required
def voice_status(request):
    account = request.user.account
    if settings.VOICE_REQUIRED:
        voice.warm_up()   # start loading the model while the user reads the screen
    return JsonResponse({
        "ok": True,
        "required": settings.VOICE_REQUIRED,
        "verified": _voice_verified(request),
        "enrolled": VoiceProfile.objects.filter(account=account).exists(),
        "engine_available": voice.engine_available(),
        "sarvam": bool(__import__("os").getenv("SARVAM_API_KEY")),
    })


@require_GET
@api_login_required
def voice_challenge(request):
    """Random digits to read aloud. Fresh every time, short-lived, single use."""
    try:
        n = min(max(int(request.GET.get("n", CHALLENGE_DIGITS)), 4), ENROLL_DIGITS)
    except ValueError:
        n = CHALLENGE_DIGITS
    digits = "".join(secrets.choice("0123456789") for _ in range(n))
    # A new challenge always replaces the old one: only the digits on screen right now count.
    request.session["voice_challenge"] = {"digits": digits, "exp": time.time() + CHALLENGE_SECONDS + CHALLENGE_GRACE}
    return JsonResponse({"ok": True, "digits": digits, "spoken": " ".join(digits), "seconds": CHALLENGE_SECONDS})


@require_POST
@api_login_required
def voice_enroll(request):
    """Save the owner's voiceprint from 3 recordings."""
    account = request.user.account
    profile = VoiceProfile.objects.filter(account=account).first()
    if profile is not None:
        # Changing an existing voiceprint needs proof: a live voice check this session,
        # or the account PIN. The password alone is not enough.
        by_voice = _voice_verified(request) and request.session.get("voice_method") == "voice"
        if not by_voice:
            pin = request.POST.get("pin")
            if not pin:
                return JsonResponse({"ok": False, "code": "voice_required",
                                     "message": "Verify your current voice or enter your PIN first."},
                                    status=403)
            checked = services.verify_pin(account, pin)
            if not checked["ok"]:
                return _respond(checked)
    try:
        embeddings = []
        for raw in _read_audio(request, ENROLL_SAMPLES):
            samples = voice.decode_wav(raw)
            voice.check_quality(samples)
            embeddings.append(voice.embed_signal(voice.prepare(samples)))
        mean = voice.mean_embedding(embeddings)
        if voice.consistency(embeddings) < MIN_CONSISTENCY:
            raise voice.VoiceError("samples_inconsistent",
                                   "The three recordings didn't sound like the same person. "
                                   "Please try again in a quiet place.")
        if voice.best_other_match(mean, account.pk) >= voice.SAME_VOICE_ELSEWHERE:
            # One voice protects one account. Otherwise this person would match two
            # voiceprints equally and be turned away from both.
            raise voice.VoiceError("voice_taken",
                                   "This voice is already set up on another EchoMint account. "
                                   "Each person's voice can protect one account.")
    except voice.VoiceError as exc:
        return _voice_problem(exc)

    threshold = voice.personal_threshold(embeddings, settings.VOICE_THRESHOLD_CAP)
    VoiceProfile.objects.update_or_create(
        account=account,
        defaults={"embedding": mean, "samples": ENROLL_SAMPLES, "threshold": threshold,
                  "failed_attempts": 0, "locked_until": None})
    _mark_verified(request, "voice")
    services._audit(account, "voice_enrolled", samples=ENROLL_SAMPLES, threshold=threshold)
    return JsonResponse({"ok": True, "code": "enrolled",
                         "message": "Your voice is saved. You're verified."})


@require_POST
@api_login_required
def voice_verify(request):
    """Check speaker + liveness (random digits) and unlock the session."""
    account = request.user.account
    profile = VoiceProfile.objects.filter(account=account).first()
    if profile is None:
        return JsonResponse({"ok": False, "code": "not_enrolled",
                             "message": "Please set up your voice first."}, status=400)
    now = timezone.now()
    if profile.locked_until and profile.locked_until > now:
        secs = int((profile.locked_until - now).total_seconds()) + 1
        return JsonResponse({"ok": False, "code": "voice_locked", "fallback_pin": True,
                             "message": f"Let's pause for {secs} seconds. You can use your PIN instead."},
                            status=423)

    challenge = request.session.pop("voice_challenge", None)   # single use
    if not challenge or challenge["exp"] < time.time():
        return JsonResponse({"ok": False, "code": "challenge_expired",
                             "message": "That took too long. Please try again."}, status=400)

    # Old voiceprints may carry a lower personal line from before; the floor always applies.
    threshold = max(profile.threshold or voice.DEFAULT_THRESHOLD, voice.THRESHOLD_FLOOR)
    expected = challenge["digits"]
    best_other = 0.0

    def fail(code, similarity, heard=None, count=True):
        if count:   # background noise ("no digits heard") never locks the owner out
            profile.failed_attempts += 1
            if profile.failed_attempts >= VOICE_LOCK_AFTER:
                profile.locked_until = now + timedelta(minutes=VOICE_LOCK_MINUTES)
                profile.failed_attempts = 0
            profile.save()
            services._audit(account, "voice_failed", reason=code, similarity=round(similarity, 3),
                            threshold=threshold, best_other=round(best_other, 3))
        body = {"ok": False, "code": code, "message": FAIL_MESSAGES[code], "fallback_pin": True}
        if heard:   # the digits are already on the user's screen, so this reveals nothing
            body["heard"] = " ".join(heard)
        if settings.DEBUG:
            body["debug_similarity"] = round(similarity * 100)
            body["debug_other"] = round(best_other * 100)
        if settings.VOICE_SHOW_SCORES:
            body["scores"] = _scores(similarity, best_other, threshold)
        return JsonResponse(body, status=400)

    stt = None
    try:
        raw = _read_audio(request, 1)[0]
        samples = voice.decode_wav(raw)
        voice.check_quality(samples)          # also rejects clips that are mostly background noise
        clean = voice.prepare(samples)
        # The digit check (speech-to-text) runs WHILE we compare voices. It gets the
        # trimmed clip: a smaller upload, faster answer, less chance of a timeout.
        stt = _pool.submit(voice.heard_digits, voice.encode_wav(clean), expected)
        emb = voice.embed_signal(clean)
        similarity = voice.cosine(profile.embedding, emb)
        # Who else could this be? Compare with every OTHER enrolled voice. If the
        # clip is as close to someone else as to the owner, it's not the owner -
        # this is what stops two people on the same laptop passing as each other.
        best_other = voice.best_other_match(emb, account.pk)
        if similarity < threshold or best_other >= similarity - voice.OTHER_VOICE_MARGIN:
            stt.cancel()
            return fail("voice_mismatch", similarity)
        try:
            spoken = stt.result()
        except voice.VoiceError as exc:
            # Both speech services unreachable (venue Wi-Fi). If the voice match is CLEAR
            # (well above the line and far from every other voice), let the owner in and
            # note it in the audit log; otherwise ask for the PIN as before.
            if (settings.VOICE_ALLOW_NO_STT and exc.code == "stt_unavailable"
                    and similarity >= threshold + CONFIDENT_MARGIN
                    and best_other < similarity - 2 * voice.OTHER_VOICE_MARGIN):
                spoken = expected
                services._audit(account, "voice_digits_skipped", reason="speech services unreachable")
            else:
                raise
    except voice.VoiceError as exc:
        if stt is not None:
            stt.cancel()
        return _voice_problem(exc)

    if not spoken:
        return fail("no_digits", similarity, count=False)
    exact = spoken == expected
    # One misheard/missed digit is forgiven once the VOICE has passed: a replayed recording
    # would say completely different digits, so this costs no security but saves retries.
    if not (exact or voice.digits_close(spoken, expected)):
        return fail("phrase_mismatch", similarity, heard=spoken)

    # The saved voiceprint is NOT changed by logins any more: learning from every pass
    # let it drift (and a wrongly accepted voice could pull it towards someone else).
    profile.failed_attempts, profile.locked_until = 0, None
    profile.save()
    _mark_verified(request, "voice")
    services._audit(account, "voice_verified", similarity=round(similarity, 3), threshold=threshold,
                    best_other=round(best_other, 3))
    body = {"ok": True, "code": "verified", "message": "Voice verified."}
    if settings.DEBUG:
        body["debug_similarity"] = round(similarity * 100)
        body["debug_other"] = round(best_other * 100)
    if settings.VOICE_SHOW_SCORES:
        body["scores"] = _scores(similarity, best_other, threshold)
    return JsonResponse(body)


def _scores(similarity, best_other, threshold) -> dict:
    """Demo mode: the numbers behind the decision, as 0-100 (shown to judges)."""
    return {"you": max(0, round(similarity * 100)), "needed": round(threshold * 100),
            "other": max(0, round(best_other * 100))}


@require_POST
@api_login_required
def voice_lock(request):
    """Lock the app again: the next action needs a fresh voice check (for live demos:
    owner unlocks -> lock -> friend tries -> rejected -> owner unlocks again)."""
    for k in ("voice_verified_at", "voice_last_active", "voice_method"):
        request.session.pop(k, None)
    return JsonResponse({"ok": True, "code": "locked", "message": "Locked. Verify your voice to continue."})


@require_POST
@api_login_required
def voice_fallback(request):
    """Backup way in when the voice check can't be passed (noisy room, mic trouble,
    speech service down): the account PIN. Same wrong-PIN lockout as payments."""
    account = request.user.account
    result = services.verify_pin(account, _body(request).get("pin"))
    if not result["ok"]:
        return _respond(result)
    _mark_verified(request, "pin")
    services._audit(account, "voice_fallback_pin")
    return JsonResponse({"ok": True, "code": "verified", "message": "PIN accepted. You're in."})


# ------------------------------------------------- Sarvam speech (from Aniket)

import base64 as _b64  # noqa: E402
import logging as _logging  # noqa: E402
import os as _os  # noqa: E402

_log = _logging.getLogger(__name__)
SARVAM_SPEAKER = _os.getenv("SARVAM_TTS_SPEAKER", "shubh_enhi_ads")


def sarvam_ready() -> bool:
    return bool(_os.getenv("SARVAM_API_KEY"))


def _sarvam_client():
    from sarvamai import SarvamAI
    return SarvamAI(api_subscription_key=_os.environ["SARVAM_API_KEY"])


def _sarvam_down(msg="Indian-language speech is not set up on the server.") -> JsonResponse:
    return JsonResponse({"ok": False, "code": "sarvam_unavailable", "message": msg}, status=503)


@require_POST
@api_login_required
def voice_stt(request):
    """Speech -> text with Sarvam (saaras:v4). Audio is forwarded and discarded,
    never stored or logged. The user's contact names are sent as hints so names
    like "Rahul Verma" are recognised more reliably."""
    if not sarvam_ready():
        return _sarvam_down()
    audio = request.FILES.get("audio")
    if audio is None or audio.size > MAX_AUDIO_BYTES * 4:
        return JsonResponse({"ok": False, "code": "no_audio", "message": "No audio received."}, status=400)
    lang = request.POST.get("lang", "").strip() or None
    data = audio.read()
    args = {"file": (audio.name or "audio.webm", data, audio.content_type or "audio/webm"),
            "model": "saaras:v4", "mode": "transcribe"}
    if lang:
        args["language_code"] = lang
    names = [c.name for c in request.user.account.contacts.all()][:30]
    try:
        client = _sarvam_client()
        try:
            resp = client.speech_to_text.transcribe(**args, keyterms=names)
        except Exception:  # noqa: BLE001  older API versions may not accept keyterms
            resp = client.speech_to_text.transcribe(**args)
        return JsonResponse({"ok": True, "transcript": resp.transcript or "",
                             "language_code": resp.language_code or lang or "en-IN"})
    except Exception:  # noqa: BLE001
        _log.exception("Sarvam STT failed")
        return JsonResponse({"ok": False, "code": "stt_error",
                             "message": "I couldn't hear that clearly. Please try again."}, status=502)


@require_POST
@api_login_required
def voice_tts(request):
    """Text -> speech with Sarvam (bulbul:v4-flash). The text is the AGENT'S reply
    (never a PIN). MP3 is requested because it is ~10x smaller than WAV, so the
    voice starts sooner; repeated phrases come from Sarvam's cache."""
    if not sarvam_ready():
        return _sarvam_down()
    data = _body(request)
    text = str(data.get("text", "")).strip()[:1000]
    lang = str(data.get("language_code", "en-IN")).strip() or "en-IN"
    if not text:
        return JsonResponse({"ok": False, "code": "no_text", "message": "No text to speak."}, status=400)
    base = {"text": text, "model": "bulbul:v4-flash", "language_code": lang, "speaker": SARVAM_SPEAKER}
    try:
        client = _sarvam_client()
        try:
            resp = client.text_to_speech.convert(**base, output_audio_codec="mp3", speech_sample_rate=22050,
                                                 enable_cached_responses=True)
            mime = "audio/mpeg"
        except Exception:  # noqa: BLE001  fall back to Aniket's tested settings
            resp = client.text_to_speech.convert(**base, speech_sample_rate=22050)
            mime = "audio/wav"
        return JsonResponse({"ok": True, "audio_b64": "".join(resp.audios), "mime": mime})
    except Exception:  # noqa: BLE001
        _log.exception("Sarvam TTS failed")
        return JsonResponse({"ok": False, "code": "tts_error", "message": "Voice reply unavailable."}, status=502)


# ---------------------------------------------------------------- sign up

@ensure_csrf_cookie
def signup_page(request):
    if request.user.is_authenticated and hasattr(request.user, "account"):
        return redirect("/app/")
    return render(request, "bank/signup.html")


@require_POST
def signup(request):
    d = _body(request)
    result = services.create_customer(d.get("full_name"), d.get("phone"), d.get("password"), d.get("pin"))
    if not result["ok"]:
        return _respond(result)
    user = authenticate(request, username=result["phone"], password=d.get("password"))
    login(request, user)
    return JsonResponse({"ok": True, "code": "signed_up", "message": result["message"],
                         "user": services.get_user_details(user.account)})


@require_POST
def password_reset(request):
    """Forgot password: mobile number + the account's 4-digit PIN -> a new password.
    Wrong PINs count towards the normal PIN lockout (3 tries, then 15 minutes)."""
    d = _body(request)
    return _respond(services.reset_password(d.get("phone"), d.get("pin"), d.get("password")))


@require_POST
@api_login_required
@voice_required
def card_freeze(request):
    return _respond(services.set_card_frozen(request.user.account, bool(_body(request).get("frozen"))))


@require_POST
@api_login_required
@voice_required
def add_contact(request):
    d = _body(request)
    return _respond(services.add_contact(request.user.account, d.get("name"), d.get("upi_id")))


@require_POST
@api_login_required
@voice_required
def remove_contact(request, contact_id):
    return _respond(services.remove_contact(request.user.account, contact_id))


# ---------------------------------------------------------------- analytics

from django.http import Http404  # noqa: E402

from .analysis import build_analysis, normalize_category  # noqa: E402
from .models import Analysis  # noqa: E402


@require_GET
@api_login_required
def analysis_live(request):
    """GET /api/analysis/?category=food&period=last_month (used by the page's selectors)."""
    q = request.GET
    return JsonResponse({"ok": True, **build_analysis(request.user.account, q.get("category"), q.get("period"))})


@require_GET
@api_login_required
def analysis_saved(request, analysis_id):
    a = Analysis.objects.filter(pk=analysis_id, account=request.user.account).first()
    if a is None:                                   # someone else's (or made-up) id
        return JsonResponse({"ok": False, "code": "not_found", "message": "Analysis not found."}, status=404)
    p = a.params or {}
    return JsonResponse({"ok": True, "params": p,
                         **build_analysis(request.user.account, p.get("category"), p.get("period"))})


@ensure_csrf_cookie
def analysis_page(request, analysis_id=None):
    if not (request.user.is_authenticated and hasattr(request.user, "account")):
        return redirect("/login/")
    params = {"category": "all", "period": "last_30_days"}
    if analysis_id is not None:
        a = Analysis.objects.filter(pk=analysis_id, account=request.user.account).first()
        if a is None:
            raise Http404("Analysis not found")
        params = {"category": normalize_category(a.params.get("category")),
                  "period": a.params.get("period") or "last_30_days"}
    return render(request, "bank/analysis.html", {"params": params, "analysis_id": analysis_id})
