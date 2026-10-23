"""JSON API used by the website (and anything else that wants to talk to the bank)."""
import json
from functools import wraps

from django.conf import settings
from django.contrib.auth import authenticate, login, logout
from django.db import connection
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST

from . import agent, services
from .models import Account

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
def start_payment(request):
    d = _body(request)
    return _respond(services.initiate_payment(
        request.user.account, d.get("recipient"), d.get("amount"),
        idempotency_key=d.get("idempotency_key")))


@require_POST
@api_login_required
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
def cancel_payment(request, payment_id):
    return _respond(services.cancel_payment(request.user.account, payment_id))


# ---------------------------------------------------------------- the website

@ensure_csrf_cookie
def index(request):
    return render(request, "bank/app.html")


MAX_HISTORY = 20


def _remember(request, role: str, content: str):
    chat = request.session.get("chat", [])
    chat.append({"role": role, "content": content})
    request.session["chat"] = chat[-MAX_HISTORY:]


@require_POST
@api_login_required
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
