"""
One-time codes (OTP) sent by SMS to the customer's real mobile number.

Used for: confirming the number at sign-up, and "Forgot password".

Pick a provider in .env (SMS_BACKEND), or leave it on "auto":
  * twilio  - Twilio Verify. Twilio creates, sends and checks the code.
              Trial accounts can only text numbers verified in the Twilio console.
  * 2factor - 2Factor.in (Indian OTP provider). They create, send and check the code.
  * console - no SMS: the code is printed in the server terminal (and shown on the
              page while DEBUG is on). Only for development / a venue with no signal.
  * auto    - twilio if its keys are set, else 2factor if its key is set, else console.

Only the standard library is used (urllib), so there is nothing extra to install.
The code itself never goes through the AI and is never logged by the real providers.
"""
import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import urllib.error
import urllib.parse
import urllib.request

from django.conf import settings

log = logging.getLogger(__name__)
TIMEOUT = 12


class SmsError(Exception):
    """The SMS provider couldn't be reached or refused the request. `message` is safe to show."""

    def __init__(self, message="We couldn't send the code right now. Please try again in a minute."):
        super().__init__(message)
        self.message = message


def backend_name() -> str:
    name = os.getenv("SMS_BACKEND", "auto").strip().lower()
    if name == "auto":
        if os.getenv("TWILIO_ACCOUNT_SID") and os.getenv("TWILIO_AUTH_TOKEN") and os.getenv("TWILIO_VERIFY_SID"):
            return "twilio"
        if os.getenv("TWOFACTOR_API_KEY"):
            return "2factor"
        return "console"
    return name


def e164(phone: str) -> str:
    """'9811122233' -> '+919811122233' (all EchoMint numbers are Indian mobiles)."""
    return "+91" + phone[-10:]


def _hash(code: str) -> str:
    return hmac.new(settings.SECRET_KEY.encode(), str(code).encode(), hashlib.sha256).hexdigest()


def _http(url, data=None, auth=None):
    headers = {"Accept": "application/json"}
    body = None
    if data is not None:
        body = urllib.parse.urlencode(data).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    if auth:
        headers["Authorization"] = "Basic " + base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()
    req = urllib.request.Request(url, data=body, headers=headers, method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read() or b"{}")
        except ValueError:
            payload = {}
        return exc.code, payload
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        log.warning("SMS provider unreachable: %s", exc)
        raise SmsError() from exc


# ------------------------------------------------------------------ providers

def _twilio_auth():
    return os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"]


def _twilio_base():
    return f"https://verify.twilio.com/v2/Services/{os.environ['TWILIO_VERIFY_SID']}"


def _twilio_send(phone):
    status, data = _http(_twilio_base() + "/Verifications", {"To": e164(phone), "Channel": "sms"}, _twilio_auth())
    if status >= 400:
        log.warning("Twilio Verify refused: %s %s", status, data)
        if data.get("code") == 21608 or "unverified" in str(data.get("message", "")).lower():
            raise SmsError("This number isn't on the Twilio trial list yet. Add it in the Twilio console first.")
        raise SmsError()
    return {"provider": "twilio"}


def _twilio_check(phone, code, ref):
    status, data = _http(_twilio_base() + "/VerificationCheck", {"To": e164(phone), "Code": code}, _twilio_auth())
    if status == 404:          # no pending code (expired or already used)
        return False
    if status >= 400:
        log.warning("Twilio VerificationCheck refused: %s %s", status, data)
        raise SmsError("We couldn't check the code right now. Please try again.")
    return data.get("status") == "approved"


def _twofactor_send(phone):
    key = urllib.parse.quote(os.environ["TWOFACTOR_API_KEY"], safe="")
    url = f"https://2factor.in/API/V1/{key}/SMS/{e164(phone)}/AUTOGEN"
    template = os.getenv("TWOFACTOR_TEMPLATE", "").strip()
    if template:
        url += "/" + urllib.parse.quote(template, safe="")
    status, data = _http(url)
    if status >= 400 or data.get("Status") != "Success":
        log.warning("2Factor refused: %s %s", status, data)
        raise SmsError()
    return {"provider": "2factor", "session": data.get("Details", "")}


def _twofactor_check(phone, code, ref):
    key = urllib.parse.quote(os.environ["TWOFACTOR_API_KEY"], safe="")
    session = urllib.parse.quote(ref.get("session", ""), safe="")
    status, data = _http(f"https://2factor.in/API/V1/{key}/SMS/VERIFY/{session}/{urllib.parse.quote(code, safe='')}")
    if status >= 500:
        raise SmsError("We couldn't check the code right now. Please try again.")
    return data.get("Status") == "Success" and "match" in str(data.get("Details", "")).lower()


def _console_send(phone):
    code = f"{secrets.randbelow(10**6):06d}"
    print(f"\n*** EchoMint OTP for {e164(phone)}: {code}  (SMS_BACKEND=console - no SMS was sent) ***\n", flush=True)
    log.warning("SMS_BACKEND=console: OTP printed in the server terminal, no SMS sent")
    ref = {"provider": "console", "hash": _hash(code)}
    if settings.DEBUG:
        ref["debug_code"] = code     # shown on the page only in DEBUG, so the demo never gets stuck
    return ref


def _console_check(phone, code, ref):
    return hmac.compare_digest(ref.get("hash", ""), _hash(code))


SENDERS = {"twilio": _twilio_send, "2factor": _twofactor_send, "console": _console_send}
CHECKERS = {"twilio": _twilio_check, "2factor": _twofactor_check, "console": _console_check}


def send_code(phone: str) -> dict:
    """Send a fresh code to this number. Returns a small reference to keep in the session."""
    name = backend_name()
    if name not in SENDERS:
        raise SmsError(f"Unknown SMS_BACKEND '{name}'. Use twilio, 2factor or console.")
    return SENDERS[name](phone)


def check_code(phone: str, code: str, ref: dict) -> bool:
    code = "".join(c for c in str(code or "") if c.isdigit())
    if not (4 <= len(code) <= 8):
        return False
    checker = CHECKERS.get(ref.get("provider"))
    return bool(checker and checker(phone, code, ref))     # provider "none" (no account): never matches
