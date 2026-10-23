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

from . import agent, services, voice
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
    message = str(_body(request).get("message", "")).strip()[:500]
    if not message:
        return JsonResponse({"ok": False, "code": "empty",
                             "message": "I didn't catch that."}, status=400)
    account = request.user.account
    out = agent.run_chat(account, request.session.get("chat", []), message)
    _remember(request, "user", message)
    _remember(request, "assistant", out["reply"])
    payload = {"ok": True, "reply": out["reply"],
               "pending_payment": agent.pending_payload(account),
               "balance": str(Account.objects.get(pk=account.pk).balance)}
    if settings.DEBUG and out["error"]:
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
CHALLENGE_SECONDS = 60
CHALLENGE_DIGITS = 6            # read aloud when logging in
ENROLL_DIGITS = 8               # longer clips when teaching the voice: much better accuracy
ENROLL_SAMPLES = 3
MIN_CONSISTENCY = 0.40          # the 3 enrollment clips must sound like one person
CONFIDENT_MARGIN = 0.10         # "clearly the owner": forgives one misheard digit
LEARN_MARGIN = 0.15             # only very clean logins refine the saved voiceprint (prevents drift)
MAX_AUDIO_BYTES = 1_500_000

_pool = ThreadPoolExecutor(max_workers=4)   # lets the speaker check and the digit check run together

FAIL_MESSAGES = {
    "voice_mismatch": "I couldn't match your voice. Speak a little closer to the mic, in your normal voice.",
    "phrase_mismatch": "I heard your voice, but not those digits. Please read the new digits clearly.",
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
    request.session["voice_challenge"] = {"digits": digits, "exp": time.time() + CHALLENGE_SECONDS}
    return JsonResponse({"ok": True, "digits": digits, "spoken": " ".join(digits)})


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

    threshold = profile.threshold or voice.DEFAULT_THRESHOLD

    def fail(code, similarity):
        profile.failed_attempts += 1
        if profile.failed_attempts >= VOICE_LOCK_AFTER:
            profile.locked_until = now + timedelta(minutes=VOICE_LOCK_MINUTES)
            profile.failed_attempts = 0
        profile.save()
        services._audit(account, "voice_failed", reason=code,
                        similarity=round(similarity, 3), threshold=threshold)
        body = {"ok": False, "code": code, "message": FAIL_MESSAGES[code], "fallback_pin": True}
        if settings.DEBUG:
            body["debug_similarity"] = round(similarity * 100)
        return JsonResponse(body, status=400)

    stt = None
    try:
        raw = _read_audio(request, 1)[0]
        samples = voice.decode_wav(raw)
        voice.check_quality(samples)
        clean = voice.prepare(samples)
        stt = _pool.submit(voice.transcribe_digits, raw)      # runs while we compare voices
        emb = voice.embed_signal(clean)
        similarity = voice.cosine(profile.embedding, emb)
        if similarity < threshold:
            stt.cancel()
            return fail("voice_mismatch", similarity)
        spoken = stt.result()
    except voice.VoiceError as exc:
        if stt is not None:
            stt.cancel()
        return _voice_problem(exc)

    confident = similarity >= threshold + CONFIDENT_MARGIN
    exact = spoken == challenge["digits"]
    if not (exact or (confident and voice.digits_close(spoken, challenge["digits"]))):
        return fail("phrase_mismatch", similarity)

    profile.failed_attempts, profile.locked_until = 0, None
    if exact and similarity >= threshold + LEARN_MARGIN:   # keep fitting this person's real mic and voice
        profile.embedding = voice.blend(profile.embedding, emb, max(profile.samples, 8))
        profile.samples = min(profile.samples + 1, 30)
    profile.save()
    _mark_verified(request, "voice")
    services._audit(account, "voice_verified", similarity=round(similarity, 3), threshold=threshold)
    body = {"ok": True, "code": "verified", "message": "Voice verified."}
    if settings.DEBUG:
        body["debug_similarity"] = round(similarity * 100)
    return JsonResponse(body)


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
