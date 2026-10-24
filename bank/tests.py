import time
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from . import services
from .models import Account, AuditLog, Contact, Payment


def make_account(phone="9000000001", balance="5000", pin="1234", name="Test User"):
    user = get_user_model().objects.create_user(username=phone, password="pass1234")
    acct = Account(user=user, full_name=name, phone=phone,
                   account_number=phone[::-1], balance=Decimal(balance))
    acct.set_pin(pin)
    acct.save()
    for n, upi in [("Rahul Sharma", "rs@upi"), ("Rahul Verma", "rv@upi"),
                   ("Mom", "mom@upi"), ("Aman", "aman@upi")]:
        Contact.objects.create(account=acct, name=n, upi_id=upi)
    return acct


class PaymentRuleTests(TestCase):
    def setUp(self):
        self.acct = make_account()

    def pay(self, who, amount, **kw):
        return services.initiate_payment(self.acct, who, amount, **kw)

    def balance(self):
        self.acct.refresh_from_db()
        return self.acct.balance

    # ---- the voice-confirm vs PIN rule -------------------------------------
    def test_under_500_needs_only_voice_confirmation(self):
        r = self.pay("Aman", 300)
        self.assertEqual(r["code"], "needs_confirmation")
        self.assertEqual(self.balance(), Decimal("5000"))  # nothing moved yet
        done = services.confirm_payment(self.acct, r["payment_id"])  # no PIN
        self.assertTrue(done["ok"], done)
        self.assertEqual(self.balance(), Decimal("4700"))

    def test_500_and_above_needs_pin(self):
        r = self.pay("Aman", 500)
        self.assertEqual(r["code"], "needs_pin")
        no_pin = services.confirm_payment(self.acct, r["payment_id"])
        self.assertEqual(no_pin["code"], "pin_required")
        self.assertEqual(self.balance(), Decimal("5000"))
        ok = services.confirm_payment(self.acct, r["payment_id"], pin="1234")
        self.assertTrue(ok["ok"], ok)
        self.assertEqual(self.balance(), Decimal("4500"))
        self.assertEqual(Payment.objects.get(pk=r["payment_id"]).auth_used, "PIN")

    def test_boundary_499_99_voice_but_500_pin(self):
        self.assertEqual(self.pay("Mom", "499.99")["code"], "needs_confirmation")
        self.assertEqual(self.pay("Mom", "500.00")["code"], "needs_pin")

    def test_wrong_pin_then_lockout(self):
        r = self.pay("Aman", 800)
        a = services.confirm_payment(self.acct, r["payment_id"], pin="0000")
        self.assertEqual((a["code"], a["attempts_left"]), ("wrong_pin", 2))
        services.confirm_payment(self.acct, r["payment_id"], pin="1111")
        c = services.confirm_payment(self.acct, r["payment_id"], pin="2222")
        self.assertEqual(c["code"], "pin_locked")
        self.assertEqual(self.balance(), Decimal("5000"))
        # even the right PIN is refused while locked
        r2 = self.pay("Aman", 800)
        d = services.confirm_payment(self.acct, r2["payment_id"], pin="1234")
        self.assertEqual(d["code"], "pin_locked")
        self.assertEqual(self.balance(), Decimal("5000"))
        # small payments (voice confirm) still work while the PIN is locked
        r3 = self.pay("Aman", 100)
        self.assertTrue(services.confirm_payment(self.acct, r3["payment_id"])["ok"])

    # ---- recipient matching -------------------------------------------------
    def test_ambiguous_and_missing_recipient(self):
        r = self.pay("Rahul", 100)
        self.assertEqual(r["code"], "ambiguous_recipient")
        self.assertCountEqual(r["options"], ["Rahul Sharma", "Rahul Verma"])
        self.assertEqual(self.pay("rahul verma", 100)["code"], "needs_confirmation")
        self.assertEqual(self.pay("mom", 100)["code"], "needs_confirmation")
        self.assertEqual(self.pay("Sundar", 100)["code"], "recipient_not_found")

    # ---- validation ---------------------------------------------------------
    def test_invalid_inputs(self):
        self.assertEqual(self.pay(None, 100)["code"], "missing_recipient")
        self.assertEqual(self.pay("Mom", None)["code"], "missing_amount")
        self.assertEqual(self.pay("Mom", 0)["code"], "invalid_amount")
        self.assertEqual(self.pay("Mom", -50)["code"], "invalid_amount")
        self.assertEqual(self.pay("Mom", "abc")["code"], "invalid_amount")

    def test_insufficient_balance(self):
        self.assertEqual(self.pay("Mom", 9999)["code"], "insufficient_balance")

    def test_balance_drops_between_start_and_confirm(self):
        r = self.pay("Aman", 300)
        Account.objects.filter(pk=self.acct.pk).update(balance=Decimal("100"))
        done = services.confirm_payment(self.acct, r["payment_id"])
        self.assertEqual(done["code"], "insufficient_balance")
        self.assertEqual(self.balance(), Decimal("100"))

    def test_daily_limit(self):
        Account.objects.filter(pk=self.acct.pk).update(daily_limit=Decimal("400"))
        self.acct.refresh_from_db()
        r = self.pay("Aman", 300)
        services.confirm_payment(self.acct, r["payment_id"])
        self.assertEqual(self.pay("Aman", 200)["code"], "daily_limit_exceeded")

    # ---- reliability --------------------------------------------------------
    def test_idempotency_key_prevents_double_payment(self):
        a = self.pay("Aman", 300, idempotency_key="abc")
        b = self.pay("Aman", 300, idempotency_key="abc")
        self.assertEqual(a["payment_id"], b["payment_id"])
        self.assertEqual(Payment.objects.count(), 1)

    def test_confirming_twice_charges_once(self):
        r = self.pay("Aman", 300)
        services.confirm_payment(self.acct, r["payment_id"])
        again = services.confirm_payment(self.acct, r["payment_id"])
        self.assertEqual(again["code"], "already_completed")
        self.assertEqual(self.balance(), Decimal("4700"))

    def test_pending_payment_expires(self):
        r = self.pay("Aman", 300)
        Payment.objects.filter(pk=r["payment_id"]).update(
            created_at=timezone.now() - timedelta(minutes=10))
        done = services.confirm_payment(self.acct, r["payment_id"])
        self.assertEqual(done["code"], "expired")
        self.assertEqual(self.balance(), Decimal("5000"))

    def test_cancel(self):
        r = self.pay("Aman", 300)
        self.assertTrue(services.cancel_payment(self.acct, r["payment_id"])["ok"])
        after = services.confirm_payment(self.acct, r["payment_id"])
        self.assertFalse(after["ok"])
        self.assertEqual(self.balance(), Decimal("5000"))

    def test_nobody_can_confirm_someone_elses_payment(self):
        other = make_account(phone="9000000002", name="Other")
        r = self.pay("Aman", 300)
        stolen = services.confirm_payment(other, r["payment_id"])
        self.assertEqual(stolen["code"], "payment_not_found")
        self.assertEqual(self.balance(), Decimal("5000"))

    def test_audit_trail_is_written(self):
        r = self.pay("Aman", 800)
        services.confirm_payment(self.acct, r["payment_id"], pin="0000")
        services.confirm_payment(self.acct, r["payment_id"], pin="1234")
        events = list(AuditLog.objects.values_list("event", flat=True))
        for e in ("payment_initiated", "wrong_pin", "payment_success"):
            self.assertIn(e, events)

    # ---- read-only queries --------------------------------------------------
    def test_history_and_summary(self):
        for who, amt in [("Aman", 100), ("Aman", 200), ("Mom", 50)]:
            r = self.pay(who, amt)
            services.confirm_payment(self.acct, r["payment_id"])
        self.pay("Mom", 10)  # left pending: must not appear anywhere
        self.assertEqual(len(services.get_history(self.acct)), 3)
        s = services.spending_summary(self.acct, recipient="Aman")
        self.assertEqual((s["total_spent"], s["payments"]), (300.0, 2))
        self.assertEqual(services.spending_summary(self.acct)["total_spent"], 350.0)


@override_settings(VOICE_REQUIRED=False)
class ApiTests(TestCase):
    def setUp(self):
        self.acct = make_account()

    def test_requires_login(self):
        self.assertEqual(self.client.get("/api/me/").status_code, 401)
        self.assertEqual(self.client.post("/api/payments/", {}, content_type="application/json").status_code, 401)

    def test_bad_login(self):
        r = self.client.post("/api/login/", {"phone": "9000000001", "password": "nope"},
                             content_type="application/json")
        self.assertEqual(r.status_code, 400)

    def test_full_flow_with_pin(self):
        j = "application/json"
        self.assertEqual(self.client.get("/api/health/").json()["ok"], True)
        login = self.client.post("/api/login/", {"phone": "9000000001", "password": "pass1234"}, content_type=j)
        self.assertEqual(login.status_code, 200)
        self.assertEqual(self.client.get("/api/me/").json()["balance"], "5000.00")

        start = self.client.post("/api/payments/", {"recipient": "Aman", "amount": 750}, content_type=j).json()
        self.assertEqual(start["code"], "needs_pin")
        url = f"/api/payments/{start['payment_id']}/confirm/"

        wrong = self.client.post(url, {"pin": "9999"}, content_type=j)
        self.assertEqual(wrong.status_code, 400)
        self.assertEqual(wrong.json()["code"], "wrong_pin")

        ok = self.client.post(url, {"pin": "1234"}, content_type=j)
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.json()["new_balance"], "4250.00")

        hist = self.client.get("/api/history/").json()["payments"]
        self.assertEqual((hist[0]["recipient"], hist[0]["amount"]), ("Aman", 750.0))
        self.assertEqual(self.client.get("/api/summary/").json()["total_spent"], 750.0)

    def test_unknown_payment_id_is_404(self):
        self.client.post("/api/login/", {"phone": "9000000001", "password": "pass1234"},
                         content_type="application/json")
        r = self.client.post("/api/payments/11111111-1111-1111-1111-111111111111/confirm/",
                             {}, content_type="application/json")
        self.assertEqual(r.status_code, 404)


# ======================================================================
# Agent tests: a scripted fake "LLM" decides which tools to call, so we can
# test the wiring and the safety rules without internet or an API key.
# ======================================================================
from unittest.mock import patch

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from . import agent


class ScriptedModel(GenericFakeChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


def scripted(*steps):
    """steps: either ("tool_name", {args}) or "final text". Each is one model reply."""
    msgs = []
    for i, s in enumerate(steps):
        if isinstance(s, tuple):
            msgs.append(AIMessage(content="", tool_calls=[
                {"name": s[0], "args": s[1], "id": f"call{i}", "type": "tool_call"}]))
        else:
            msgs.append(AIMessage(content=s))
    return ScriptedModel(messages=iter(msgs))


class AgentTests(TestCase):
    def setUp(self):
        self.acct = make_account()

    def chat(self, message, *steps, history=None):
        with patch.object(agent, "get_model", return_value=scripted(*steps)):
            return agent.run_chat(self.acct, history or [], message)

    def balance(self):
        self.acct.refresh_from_db()
        return self.acct.balance

    def test_small_payment_voice_flow(self):
        out = self.chat("send 300 to aman",
                        ("start_payment", {"recipient_name": "Aman", "amount": 300}),
                        "Confirm sending ₹300 to Aman?")
        self.assertIsNone(out["error"])
        self.assertEqual(out["reply"], "Confirm sending ₹300 to Aman?")
        self.assertEqual(self.balance(), Decimal("5000"))          # not yet
        pending = agent.pending_payload(self.acct)
        self.assertFalse(pending["needs_pin"])

        out = self.chat("yes", ("confirm_pending_payment", {}), "Done, ₹300 sent to Aman.")
        self.assertEqual(self.balance(), Decimal("4700"))
        self.assertIsNone(agent.pending_payload(self.acct))

    def test_ai_cannot_complete_a_pin_payment(self):
        self.chat("send 800 to aman",
                  ("start_payment", {"recipient_name": "Aman", "amount": 800}),
                  "Please enter your PIN on screen.")
        self.assertTrue(agent.pending_payload(self.acct)["needs_pin"])
        # Even if the model (wrongly) tries to confirm, no money moves.
        self.chat("yes", ("confirm_pending_payment", {}), "Please use the PIN pad.")
        self.assertEqual(self.balance(), Decimal("5000"))
        self.assertEqual(Payment.objects.filter(status="SUCCESS").count(), 0)
        # The website path (PIN typed on screen) is what completes it.
        pid = agent.pending_payload(self.acct)["id"]
        self.assertTrue(services.confirm_payment(self.acct, pid, pin="1234")["ok"])
        self.assertEqual(self.balance(), Decimal("4200"))

    def test_new_request_replaces_old_pending(self):
        self.chat("send 100 to aman", ("start_payment", {"recipient_name": "Aman", "amount": 100}), "Confirm?")
        self.chat("actually mom 200", ("start_payment", {"recipient_name": "Mom", "amount": 200}), "Confirm?")
        self.assertEqual(Payment.objects.filter(status="PENDING").count(), 1)
        self.assertEqual(agent.pending_payload(self.acct)["recipient"], "Mom")

    def test_cancel(self):
        self.chat("send 100 to aman", ("start_payment", {"recipient_name": "Aman", "amount": 100}), "Confirm?")
        self.chat("no", ("cancel_pending_payment", {}), "Cancelled.")
        self.assertIsNone(agent.pending_payload(self.acct))
        self.assertEqual(self.balance(), Decimal("5000"))

    def test_read_only_tools(self):
        r = self.chat("balance?", ("get_user_details", {}), "Your balance is ₹5000.")
        self.assertEqual(r["reply"], "Your balance is ₹5000.")
        r = self.chat("total to aman", ("get_spending_summary", {"recipient_name": "Aman"}), "Nothing yet.")
        self.assertIsNone(r["error"])
        r = self.chat("history", ("get_payment_history", {}), "No payments yet.")
        self.assertIsNone(r["error"])

    def test_tools_only_touch_the_logged_in_account(self):
        other = make_account(phone="9000000002", name="Other", balance="777")
        tools = {t.name: t for t in agent.build_tools(self.acct)}
        from asgiref.sync import async_to_sync
        out = async_to_sync(tools["get_user_details"].ainvoke)({})
        self.assertIn("5000", out)
        self.assertNotIn("777", out)
        self.assertEqual(other.payments.count(), 0)

    def test_failure_gives_spoken_fallback_not_a_crash(self):
        with patch.object(agent, "get_model", side_effect=RuntimeError("API down")):
            out = agent.run_chat(self.acct, [], "hello")
        self.assertEqual(out["reply"], agent.FALLBACK_REPLY)
        self.assertIn("API down", out["error"])


@override_settings(VOICE_REQUIRED=False)
class WebTests(TestCase):
    def setUp(self):
        self.acct = make_account()

    def test_home_page_renders(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "A hands-free bank for the elderly")
        self.assertContains(r, "bank-machine.js")

    def test_login_page_and_app_routing(self):
        self.assertContains(self.client.get("/login/"), "loginForm")
        r = self.client.get("/app/")
        self.assertRedirects(r, "/login/", fetch_redirect_response=False)       # app needs a login
        self.client.post("/api/login/", {"phone": "9000000001", "password": "pass1234"},
                         content_type="application/json")
        self.assertContains(self.client.get("/app/"), "voiceOverlay")
        self.assertRedirects(self.client.get("/login/"), "/app/", fetch_redirect_response=False)

    def test_chat_endpoint_end_to_end(self):
        j = "application/json"
        self.assertEqual(self.client.post("/api/agent/chat/", {"message": "hi"}, content_type=j).status_code, 401)
        self.client.post("/api/login/", {"phone": "9000000001", "password": "pass1234"}, content_type=j)
        # a phrasing the instant path doesn't know goes to the AI agent
        with patch.object(agent, "get_model", return_value=scripted(
                ("start_payment", {"recipient_name": "Aman", "amount": 900}), "Enter your PIN.")):
            r = self.client.post("/api/agent/chat/", {"message": "could you kindly arrange nine hundred for aman"},
                                 content_type=j).json()
        self.assertEqual((r["reply"], r["source"]), ("Enter your PIN.", "ai"))
        self.assertTrue(r["pending_payment"]["needs_pin"])
        # The website then sends the PIN straight to Django (never through the AI).
        url = f"/api/payments/{r['pending_payment']['id']}/confirm/"
        done = self.client.post(url, {"pin": "1234"}, content_type=j).json()
        self.assertTrue(done["ok"])
        # ...and the agent's memory now knows the payment is done.
        self.assertIn("successfully transferred", self.client.session["chat"][-1]["content"])

    def test_empty_message_rejected(self):
        j = "application/json"
        self.client.post("/api/login/", {"phone": "9000000001", "password": "pass1234"}, content_type=j)
        self.assertEqual(self.client.post("/api/agent/chat/", {"message": "  "}, content_type=j).status_code, 400)


class ResilienceTests(TestCase):
    def setUp(self):
        self.acct = make_account()

    def test_backup_model_is_used_when_main_model_fails(self):
        names = agent.model_names()
        self.assertEqual(len(names), 2)

        def flaky(name=None):
            if name == names[0]:
                raise RuntimeError("504 DEADLINE_EXCEEDED")
            return scripted("Your balance is ₹5000.")

        with patch.object(agent, "get_model", side_effect=flaky):
            out = agent.run_chat(self.acct, [], "balance?")
        self.assertEqual(out["reply"], "Your balance is ₹5000.")
        self.assertIsNone(out["error"])

    def test_retry_with_backup_never_double_charges(self):
        # Main model starts + confirms a payment, then dies; the backup replays the turn.
        r = services.initiate_payment(self.acct, "Aman", 300)
        services.confirm_payment(self.acct, r["payment_id"])
        tools = {t.name: t for t in agent.build_tools(self.acct)}
        from asgiref.sync import async_to_sync
        again = async_to_sync(tools["confirm_pending_payment"].ainvoke)({})
        self.assertIn("no payment waiting", again.lower())
        self.acct.refresh_from_db()
        self.assertEqual(self.acct.balance, Decimal("4700"))

    def test_database_outage_returns_friendly_json_not_an_error_page(self):
        from django.db.utils import OperationalError
        j = "application/json"
        self.client.post("/api/login/", {"phone": "9000000001", "password": "pass1234"}, content_type=j)
        with patch.object(services, "get_user_details", side_effect=OperationalError("could not translate host name")):
            r = self.client.get("/api/me/")
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.json()["code"], "database_unavailable")
        self.assertIn("money is safe", r.json()["message"])



# ======================================================================
# Voice security tests. The heavy voice model and Gemini are replaced by
# fakes, so these run fast and offline. A "voiceprint" here is just a vector.
# ======================================================================
import io
import wave

import numpy as np

from . import voice
from .models import VoiceProfile

OWNER = np.array([1.0, 0.2, 0.1, 0.0])
OWNER_AGAIN = np.array([0.98, 0.25, 0.12, 0.02])     # same person, slightly different
STRANGER = np.array([0.0, 0.1, 1.0, 0.3])            # clearly a different voice


def wav_bytes(seconds=3.0, amplitude=0.2, rate=16000, pad=0.5, noise=0.0):
    """A test 'voice' clip: a tone with quiet gaps before and after, like a real recording."""
    t = np.arange(int(seconds * rate)) / rate
    tone = amplitude * np.sin(2 * np.pi * 220 * t)
    gap = np.zeros(int(pad * rate))
    x = np.concatenate([gap, tone, gap])
    if noise:
        x = x + np.random.default_rng(0).normal(0, noise, len(x))
    data = (np.clip(x, -1, 1) * 32767).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate); w.writeframes(data.tobytes())
    return buf.getvalue()


def clip(name="clip.wav", **kw):
    from django.core.files.uploadedfile import SimpleUploadedFile
    return SimpleUploadedFile(name, wav_bytes(**kw), content_type="audio/wav")


class VoiceTests(TestCase):
    def setUp(self):
        self.acct = make_account()
        self.client.post("/api/login/", {"phone": "9000000001", "password": "pass1234"},
                         content_type="application/json")

    # helpers -----------------------------------------------------------
    def enroll(self, vectors=(OWNER, OWNER_AGAIN, OWNER)):
        with patch.object(voice, "embed_signal", side_effect=list(vectors)):
            return self.client.post("/api/voice/enroll/", {"audio": [clip(), clip(), clip()]})

    def verify(self, vector, spoken=None, digits=None):
        ch = self.client.get("/api/voice/challenge/").json()
        spoken = ch["digits"] if spoken is None else spoken
        with patch.object(voice, "embed_signal", return_value=vector), \
             patch.object(voice, "transcribe_digits", return_value=spoken):
            return self.client.post("/api/voice/verify/", {"audio": clip()})

    def chat(self):
        with patch.object(agent, "get_model", return_value=scripted("Hello!")):
            return self.client.post("/api/agent/chat/", {"message": "hi"}, content_type="application/json")

    # tests -------------------------------------------------------------
    def test_nothing_works_until_voice_is_verified(self):
        self.assertEqual(self.chat().status_code, 403)
        self.assertEqual(self.chat().json()["code"], "voice_required")
        j = "application/json"
        self.assertEqual(self.client.post("/api/payments/", {"recipient": "Aman", "amount": 10}, content_type=j).status_code, 403)
        # reading account info is still allowed (needed to show the page)
        self.assertEqual(self.client.get("/api/me/").status_code, 200)

    def test_status_reports_enrollment(self):
        st = self.client.get("/api/voice/status/").json()
        self.assertEqual((st["required"], st["verified"], st["enrolled"]), (True, False, False))
        self.assertEqual(self.enroll().status_code, 200)
        st = self.client.get("/api/voice/status/").json()
        self.assertEqual((st["verified"], st["enrolled"]), (True, True))

    def test_enroll_saves_voiceprint_and_unlocks(self):
        r = self.enroll()
        self.assertTrue(r.json()["ok"], r.json())
        self.assertEqual(VoiceProfile.objects.get(account=self.acct).samples, 3)
        self.assertEqual(self.chat().status_code, 200)

    def test_enroll_rejects_silence_and_short_audio(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        silent = SimpleUploadedFile("s.wav", wav_bytes(amplitude=0.0), content_type="audio/wav")
        r = self.client.post("/api/voice/enroll/", {"audio": [silent, clip(), clip()]})
        self.assertEqual(r.json()["code"], "audio_silent")
        short = SimpleUploadedFile("x.wav", wav_bytes(seconds=0.5), content_type="audio/wav")
        r = self.client.post("/api/voice/enroll/", {"audio": [short, clip(), clip()]})
        self.assertEqual(r.json()["code"], "audio_too_short")
        self.assertFalse(VoiceProfile.objects.exists())

    def test_enroll_needs_three_clips_and_consistent_voice(self):
        r = self.client.post("/api/voice/enroll/", {"audio": [clip()]})
        self.assertEqual(r.json()["code"], "audio_invalid")
        r = self.enroll(vectors=(OWNER, STRANGER, OWNER))
        self.assertEqual(r.json()["code"], "samples_inconsistent")

    def test_owner_with_right_digits_is_verified(self):
        self.enroll(); self.client.session.flush
        s = self.client.session; s.pop("voice_verified_at", None); s.save()   # simulate a new login
        self.assertEqual(self.chat().status_code, 403)
        r = self.verify(OWNER_AGAIN)
        self.assertTrue(r.json()["ok"], r.json())
        self.assertNotIn("similarity", r.json())          # users never see a score
        self.assertEqual(self.chat().status_code, 200)

    def _fresh_session(self):
        s = self.client.session; s.pop("voice_verified_at", None); s.save()

    def test_stranger_is_rejected_even_with_correct_digits(self):
        self.enroll(); self._fresh_session()
        r = self.verify(STRANGER)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["code"], "voice_mismatch")
        self.assertEqual(self.chat().status_code, 403)

    def test_replayed_recording_fails_the_digit_check(self):
        # Right voice (a recording of the owner!) but it says the WRONG digits.
        self.enroll(); self._fresh_session()
        r = self.verify(OWNER, spoken="0000")
        self.assertEqual(r.json()["code"], "phrase_mismatch")
        self.assertEqual(self.chat().status_code, 403)

    def test_challenge_is_single_use(self):
        self.enroll(); self._fresh_session()
        ch = self.client.get("/api/voice/challenge/").json()
        with patch.object(voice, "embed_signal", return_value=OWNER), \
             patch.object(voice, "transcribe_digits", return_value="0000"):
            self.client.post("/api/voice/verify/", {"audio": clip()})       # uses up the challenge
        with patch.object(voice, "embed_signal", return_value=OWNER), \
             patch.object(voice, "transcribe_digits", return_value=ch["digits"]):
            r = self.client.post("/api/voice/verify/", {"audio": clip()})   # old digits can't be reused
        self.assertEqual(r.json()["code"], "challenge_expired")

    def test_lockout_after_five_failures(self):
        self.enroll(); self._fresh_session()
        for _ in range(5):
            self.assertEqual(self.verify(STRANGER).json()["code"], "voice_mismatch")
        r = self.verify(OWNER)                      # even the real owner must now wait
        self.assertEqual(r.status_code, 423)
        self.assertEqual(r.json()["code"], "voice_locked")

    def test_password_alone_cannot_replace_the_voiceprint(self):
        self.enroll(); self._fresh_session()
        r = self.enroll(vectors=(STRANGER, STRANGER, STRANGER))
        self.assertEqual(r.status_code, 403)
        self.assertEqual(VoiceProfile.objects.get(account=self.acct).embedding[0], voice.mean_embedding([OWNER, OWNER_AGAIN, OWNER])[0])

    def test_verification_expires(self):
        self.enroll()
        self.assertEqual(self.chat().status_code, 200)
        s = self.client.session
        s["voice_verified_at"] = time.time() - 60 * 60
        s["voice_last_active"] = time.time() - 45 * 60          # idle for 45 minutes
        s.save()
        self.assertEqual(self.chat().status_code, 403)

    def test_switch_off_for_emergencies(self):
        with override_settings(VOICE_REQUIRED=False):
            self.assertEqual(self.chat().status_code, 200)

    def test_missing_engine_gives_clear_error(self):
        with patch.object(voice, "engine_available", return_value=False):
            r = self.client.post("/api/voice/enroll/", {"audio": [clip(), clip(), clip()]})
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.json()["code"], "engine_missing")

    def test_audit_trail_records_voice_events(self):
        self.enroll(); self._fresh_session()
        self.verify(STRANGER); self.verify(OWNER)
        events = set(AuditLog.objects.values_list("event", flat=True))
        self.assertTrue({"voice_enrolled", "voice_failed", "voice_verified"} <= events)


class VoiceAudioUnitTests(TestCase):
    def test_decode_resamples_and_mixes_to_mono(self):
        samples = voice.decode_wav(wav_bytes(seconds=2, rate=48000, pad=0))
        self.assertAlmostEqual(len(samples) / voice.SAMPLE_RATE, 2.0, places=1)

    def test_garbage_is_rejected(self):
        with self.assertRaises(voice.VoiceError):
            voice.decode_wav(b"not a wav file")

    def test_cosine_and_mean(self):
        self.assertAlmostEqual(voice.cosine(OWNER, OWNER), 1.0)
        self.assertLess(voice.cosine(OWNER, STRANGER), 0.4)
        self.assertAlmostEqual(float(np.linalg.norm(voice.mean_embedding([OWNER, STRANGER]))), 1.0)


def vec(c):
    """A unit vector whose cosine with [1,0,0,0] is exactly c."""
    return np.array([c, (1 - c * c) ** 0.5, 0.0, 0.0])


class VoiceTuningTests(TestCase):
    """Accuracy/usability: personal threshold, digit tolerance, learning, PIN backup."""

    def setUp(self):
        self.acct = make_account()          # PIN is 1234 in make_account
        self.client.post("/api/login/", {"phone": "9000000001", "password": "pass1234"},
                         content_type="application/json")
        VoiceProfile.objects.create(account=self.acct, embedding=[1, 0, 0, 0], samples=3, threshold=0.40)
        s = self.client.session; s["voice_verified_at"] = 0; s.save()
        self.acct_pin = "1234"

    def fresh(self):
        s = self.client.session
        for k in ("voice_verified_at", "voice_last_active", "voice_method"):
            s.pop(k, None)
        s.save()

    def verify(self, similarity, spoken=None):
        ch = self.client.get("/api/voice/challenge/").json()
        spoken = ch["digits"] if spoken is None else spoken
        with patch.object(voice, "embed_signal", return_value=vec(similarity)), \
             patch.object(voice, "transcribe_digits", return_value=spoken):
            return self.client.post("/api/voice/verify/", {"audio": clip()}), ch["digits"]

    # --- threshold -----------------------------------------------------
    def test_challenge_length(self):
        self.assertEqual(len(self.client.get("/api/voice/challenge/").json()["digits"]), 8)
        self.assertEqual(len(self.client.get("/api/voice/challenge/?n=8").json()["digits"]), 8)
        self.assertEqual(len(self.client.get("/api/voice/challenge/?n=99").json()["digits"]), 8)

    def test_personal_threshold_is_bounded(self):
        clean = [OWNER, OWNER_AGAIN, OWNER]                          # very consistent voice
        self.assertEqual(voice.personal_threshold(clean, cap=0.42), 0.42)
        noisy = [vec(1.0), vec(0.5), np.array([0.5, 0.0, 0.87, 0.0])]
        self.assertGreaterEqual(voice.personal_threshold(noisy, cap=0.42), voice.THRESHOLD_FLOOR)

    def test_enroll_stores_the_personal_threshold(self):
        VoiceProfile.objects.all().delete()
        with patch.object(voice, "embed_signal", side_effect=[OWNER, OWNER_AGAIN, OWNER]):
            r = self.client.post("/api/voice/enroll/", {"audio": [clip(), clip(), clip()]})
        self.assertTrue(r.json()["ok"], r.json())
        from django.conf import settings as dj
        self.assertEqual(VoiceProfile.objects.get(account=self.acct).threshold, dj.VOICE_THRESHOLD_CAP)

    def test_borderline_owner_now_gets_in_but_stranger_does_not(self):
        r, _ = self.verify(0.52); self.assertTrue(r.json()["ok"], r.json())     # the old 0.65 line rejected this
        self.fresh()
        r, _ = self.verify(0.20); self.assertEqual(r.json()["code"], "voice_mismatch")

    def test_scores_are_hidden_from_users(self):
        r, _ = self.verify(0.2)
        self.assertNotIn("similarity", r.json()); self.assertNotIn("debug_similarity", r.json())
        self.assertIn("fallback_pin", r.json())

    # --- digits ---------------------------------------------------------
    def test_digit_helpers(self):
        self.assertEqual(voice.only_digits("४ 7 २ 9"), "4729")                 # Hindi numerals
        self.assertTrue(voice.digits_close("123456", "123456"))
        self.assertTrue(voice.digits_close("123457", "123456"))     # one misheard digit
        self.assertTrue(voice.digits_close("12345", "123456"))      # one missed digit
        self.assertTrue(voice.digits_close("1234566", "123456"))    # one extra digit
        self.assertFalse(voice.digits_close("123477", "123456"))
        self.assertFalse(voice.digits_close("1234", "123456"))
        self.assertFalse(voice.digits_close("", "123456"))

    def test_one_wrong_digit_is_forgiven_once_the_voice_has_passed(self):
        ch = self.client.get("/api/voice/challenge/").json()["digits"]
        near = ch[:-1] + str((int(ch[-1]) + 1) % 10)
        with patch.object(voice, "embed_signal", return_value=vec(0.80)), \
             patch.object(voice, "transcribe_digits", return_value=near):
            self.assertTrue(self.client.post("/api/voice/verify/", {"audio": clip()}).json()["ok"])
        self.fresh()
        ch = self.client.get("/api/voice/challenge/").json()["digits"]
        near = ch[:-1] + str((int(ch[-1]) + 1) % 10)
        with patch.object(voice, "embed_signal", return_value=vec(0.45)), \
             patch.object(voice, "transcribe_digits", return_value=near):                  # voice passed, 1 slip
            self.assertTrue(self.client.post("/api/voice/verify/", {"audio": clip()}).json()["ok"])
        self.fresh()
        ch = self.client.get("/api/voice/challenge/").json()["digits"]
        with patch.object(voice, "embed_signal", return_value=vec(0.45)), \
             patch.object(voice, "transcribe_digits", return_value=ch[:-2] + "00" if ch[-2:] != "00" else ch[:-2] + "11"):
            self.assertEqual(self.client.post("/api/voice/verify/", {"audio": clip()}).json()["code"], "phrase_mismatch")

    def test_totally_wrong_digits_always_fail(self):
        r, _ = self.verify(0.95, spoken="000000")
        self.assertEqual(r.json()["code"], "phrase_mismatch")

    # --- learning ------------------------------------------------------
    def test_logins_never_change_the_saved_voiceprint(self):
        before = VoiceProfile.objects.get(account=self.acct)
        self.assertTrue(self.verify(0.80)[0].json()["ok"])
        after = VoiceProfile.objects.get(account=self.acct)
        self.assertEqual((after.embedding, after.samples), (before.embedding, before.samples))

    # --- not getting stuck ---------------------------------------------
    def test_speech_service_outage_is_not_the_users_fault(self):
        ch = self.client.get("/api/voice/challenge/").json()
        err = voice.VoiceError("stt_unavailable", "down")
        with patch.object(voice, "embed_signal", return_value=vec(0.45)), \
             patch.object(voice, "transcribe_digits", side_effect=err):
            r = self.client.post("/api/voice/verify/", {"audio": clip()})
        self.assertEqual(r.status_code, 503); self.assertTrue(r.json()["fallback_pin"])
        self.assertEqual(VoiceProfile.objects.get(account=self.acct).failed_attempts, 0)

    def test_pin_fallback_unlocks_the_app(self):
        self.fresh()
        j = "application/json"
        self.assertEqual(self.client.post("/api/voice/fallback/", {"pin": "9999"}, content_type=j).json()["code"], "wrong_pin")
        r = self.client.post("/api/voice/fallback/", {"pin": self.acct_pin}, content_type=j)
        self.assertTrue(r.json()["ok"], r.json())
        self.assertTrue(self.client.get("/api/voice/status/").json()["verified"])

    def test_pin_fallback_locks_after_three_wrong_pins(self):
        j = "application/json"
        for _ in range(3):
            self.client.post("/api/voice/fallback/", {"pin": "0000"}, content_type=j)
        r = self.client.post("/api/voice/fallback/", {"pin": self.acct_pin}, content_type=j)
        self.assertEqual(r.status_code, 423)

    def test_pin_session_cannot_swap_the_voiceprint_without_the_pin(self):
        self.fresh()
        j = "application/json"
        self.client.post("/api/voice/fallback/", {"pin": self.acct_pin}, content_type=j)    # verified by PIN
        with patch.object(voice, "embed_signal", side_effect=[STRANGER] * 3):
            r = self.client.post("/api/voice/enroll/", {"audio": [clip(), clip(), clip()]})
        self.assertEqual(r.status_code, 403)

    def test_owner_can_rerecord_voice_with_pin(self):
        self.fresh()
        with patch.object(voice, "embed_signal", side_effect=[OWNER, OWNER_AGAIN, OWNER]):
            r = self.client.post("/api/voice/enroll/", {"audio": [clip(), clip(), clip()], "pin": self.acct_pin})
        self.assertTrue(r.json()["ok"], r.json())
        self.fresh()
        with patch.object(voice, "embed_signal", side_effect=[OWNER, OWNER_AGAIN, OWNER]):
            r = self.client.post("/api/voice/enroll/", {"audio": [clip(), clip(), clip()], "pin": "0000"})
        self.assertFalse(r.json()["ok"])

    # --- sessions and audio ---------------------------------------------
    def test_session_stays_unlocked_while_active_and_expires_when_idle(self):
        now = time.time()
        s = self.client.session
        s["voice_verified_at"] = now - 2 * 3600; s["voice_last_active"] = now - 20 * 60; s.save()
        self.assertTrue(self.client.get("/api/voice/status/").json()["verified"])        # refresh is fine
        s = self.client.session; s["voice_last_active"] = now - 40 * 60; s.save()
        self.assertFalse(self.client.get("/api/voice/status/").json()["verified"])
        s = self.client.session; s["voice_verified_at"] = now - 13 * 3600; s["voice_last_active"] = now; s.save()
        self.assertFalse(self.client.get("/api/voice/status/").json()["verified"])       # hard limit

    def test_prepare_trims_silence_and_evens_loudness(self):
        rate = voice.SAMPLE_RATE
        tone = 0.05 * np.sin(2 * np.pi * 220 * np.arange(2 * rate) / rate)
        padded = np.concatenate([np.zeros(rate), tone, np.zeros(rate)]).astype(np.float32)
        out = voice.prepare(padded)
        self.assertAlmostEqual(len(out) / rate, 2.2, delta=0.15)
        self.assertAlmostEqual(float(np.sqrt(np.mean(out ** 2))), 0.1, delta=0.02)

    def test_prepare_rejects_too_little_speech(self):
        rate = voice.SAMPLE_RATE
        blip = 0.2 * np.sin(2 * np.pi * 220 * np.arange(int(0.3 * rate)) / rate)
        audio = np.concatenate([np.zeros(rate), blip, np.zeros(rate)]).astype(np.float32)
        with self.assertRaises(voice.VoiceError):
            voice.prepare(audio)

    def test_speech_to_text_falls_back_to_the_backup_model(self):
        from types import SimpleNamespace
        calls = []

        def fake_generate(model, contents):
            calls.append(model)
            if len(calls) == 1:
                raise RuntimeError("503 UNAVAILABLE")
            return SimpleNamespace(text="4 7 2 9 1 3")
        with patch("google.genai.Client") as client:
            client.return_value.models.generate_content.side_effect = fake_generate
            self.assertEqual(voice.transcribe_digits(wav_bytes()), "472913")
        self.assertEqual(len(calls), 2); self.assertNotEqual(calls[0], calls[1])


@override_settings(VOICE_REQUIRED=False)
class DashboardApiTests(TestCase):
    """Endpoints behind the new dashboard: notifications, PIN change, daily limit, bills."""

    def setUp(self):
        self.acct = make_account()
        Contact.objects.create(account=self.acct, name="Adani Electricity", upi_id="a@bill", kind="biller")
        self.client.post("/api/login/", {"phone": "9000000001", "password": "pass1234"},
                         content_type="application/json")
        self.j = "application/json"

    def post(self, url, body):
        return self.client.post(url, body, content_type=self.j)

    def test_me_has_contact_kinds_and_spent_today(self):
        me = self.client.get("/api/me/").json()
        self.assertIn("spent_today", me)
        self.assertIn({"name": "Adani Electricity", "upi_id": "a@bill", "kind": "biller"},
                      [{k: c[k] for k in ("name", "upi_id", "kind")} for c in me["contacts"]])
        self.assertTrue(all(isinstance(c["id"], int) for c in me["contacts"]))

    def test_bill_payment_follows_the_same_pin_rule(self):
        r = self.post("/api/payments/", {"recipient": "Adani Electricity", "amount": 1200})
        self.assertEqual(r.json()["code"], "needs_pin")
        r = self.post(f"/api/payments/{r.json()['payment_id']}/confirm/", {"pin": "1234"})
        self.assertTrue(r.json()["ok"], r.json())

    def test_notifications_list_payments_and_wrong_pins(self):
        r = self.post("/api/payments/", {"recipient": "Aman", "amount": 100})
        self.post(f"/api/payments/{r.json()['payment_id']}/confirm/", {})
        r = self.post("/api/payments/", {"recipient": "Aman", "amount": 900})
        self.post(f"/api/payments/{r.json()['payment_id']}/confirm/", {"pin": "0000"})
        texts = [n["text"] for n in self.client.get("/api/notifications/").json()["notifications"]]
        self.assertIn("Paid ₹100 to Aman", texts)
        self.assertIn("A wrong PIN was entered", texts)

    def test_change_pin(self):
        self.assertEqual(self.post("/api/pin/change/", {"old_pin": "9999", "new_pin": "5829"}).json()["code"], "wrong_pin")
        self.assertEqual(self.post("/api/pin/change/", {"old_pin": "1234", "new_pin": "1111"}).json()["code"], "weak_pin")
        self.assertTrue(self.post("/api/pin/change/", {"old_pin": "1234", "new_pin": "5829"}).json()["ok"])
        self.acct.refresh_from_db()
        self.assertTrue(self.acct.check_pin("5829"))

    def test_daily_limit_needs_pin_and_sane_range(self):
        self.assertEqual(self.post("/api/settings/daily-limit/", {"daily_limit": 5000, "pin": "0000"}).json()["code"], "wrong_pin")
        self.assertEqual(self.post("/api/settings/daily-limit/", {"daily_limit": 10, "pin": "1234"}).json()["code"], "invalid_amount")
        self.assertTrue(self.post("/api/settings/daily-limit/", {"daily_limit": 5000, "pin": "1234"}).json()["ok"])
        self.acct.refresh_from_db()
        self.assertEqual(self.acct.daily_limit, Decimal("5000"))

    def test_dashboard_page_has_every_section(self):
        html = self.client.get("/app/").content.decode()
        for v in ["dashboard", "accounts", "payments", "investments", "cards", "loans", "support", "profile", "settings"]:
            self.assertIn(f'data-view="{v}"', html)
        for el in ["pinOverlay", "voiceOverlay", "composer", "reVoice"]:
            self.assertIn(f'id="{el}"', html)


# ======================================================================= analytics
from datetime import date, timedelta as _td  # noqa: E402

from django.core.management import call_command  # noqa: E402
from django.utils import timezone as _tz  # noqa: E402

import json  # noqa: E402

from asgiref.sync import async_to_sync  # noqa: E402

from . import analysis, intents, views  # noqa: E402
from .categories import categorize  # noqa: E402
from .models import Analysis  # noqa: E402


def paid(acct, name, amount, days_ago=1, kind="merchant"):
    c, _ = Contact.objects.get_or_create(account=acct, name=name, defaults={"upi_id": f"{name.lower().replace(' ', '')}@upi", "kind": kind})
    p = Payment.objects.create(account=acct, contact=c, recipient_name=name, upi_id=c.upi_id, amount=Decimal(str(amount)),
                               status=Payment.Status.SUCCESS, auth_required="VOICE_CONFIRM", auth_used="VOICE_CONFIRM",
                               category=categorize(name, kind))
    when = _tz.now() - _td(days=days_ago)
    Payment.objects.filter(pk=p.pk).update(created_at=when, completed_at=when)
    return p


class CategoryTests(TestCase):
    def test_rules(self):
        self.assertEqual(categorize("Swiggy"), "food")
        self.assertEqual(categorize("Uber India"), "travel")
        self.assertEqual(categorize("BigBasket"), "groceries")
        self.assertEqual(categorize("Apollo Pharmacy"), "health")
        self.assertEqual(categorize("Adani Electricity", "biller"), "bills")
        self.assertEqual(categorize("Rahul Sharma", "person"), "transfers")
        self.assertEqual(categorize("Some Shop"), "other")

    def test_new_payments_are_categorised(self):
        acct = make_account()
        Contact.objects.create(account=acct, name="Zomato", upi_id="z@upi", kind="merchant")
        r = services.initiate_payment(acct, "Zomato", 200)
        self.assertEqual(Payment.objects.get(pk=r["payment_id"]).category, "food")

    def test_backfill_command(self):
        acct = make_account()
        p = paid(acct, "Netflix", 199)
        Payment.objects.filter(pk=p.pk).update(category="other")
        call_command("backfill_categories", stdout=io.StringIO())
        self.assertEqual(Payment.objects.get(pk=p.pk).category, "entertainment")


class AnalysisTests(TestCase):
    def setUp(self):
        self.acct = make_account()
        for i in range(12):
            paid(self.acct, "Swiggy" if i % 2 else "Zomato", 100 + i * 10, days_ago=1 + i * 2)
        paid(self.acct, "Uber", 300, days_ago=3)
        paid(self.acct, "Uber", 50, days_ago=40)           # previous period
        other = make_account(phone="9000000002")
        paid(other, "Swiggy", 99999, days_ago=2)            # never counted for self.acct

    def test_totals_match_the_database(self):
        spec = analysis.build_analysis(self.acct, "food", "last_30_days")
        db = sum(p.amount for p in Payment.objects.filter(account=self.acct, category="food",
                 created_at__date__gte=_tz.localdate() - _td(days=29)))
        total = next(k for k in spec["kpis"] if k["id"] == "total")
        self.assertEqual(Decimal(str(total["raw"])), db)
        self.assertNotIn("99,999", json.dumps(spec))

    def test_period_parsing(self):
        today = date(2026, 10, 23)
        self.assertEqual(analysis.parse_period("last_month", today)[:2], (date(2026, 9, 1), date(2026, 9, 30)))
        self.assertEqual(analysis.parse_period("this month", today)[:2], (date(2026, 10, 1), today))
        self.assertEqual(analysis.parse_period("last 7 days", today)[:2], (date(2026, 10, 17), today))
        self.assertEqual(analysis.parse_period("this_year", today)[:2], (date(2026, 1, 1), today))
        self.assertEqual(analysis.parse_period("last_year", today)[:2], (date(2025, 1, 1), date(2025, 12, 31)))
        self.assertEqual(analysis.parse_period({"start": "2026-02-01", "end": "2026-02-10"}, today)[:2], (date(2026, 2, 1), date(2026, 2, 10)))
        self.assertEqual(analysis.parse_period("nonsense", today)[3], "last_30_days")
        self.assertEqual(analysis.parse_period("last 3 months", today)[3], "last_3_months")

    def test_chart_selection_depends_on_the_data(self):
        food = [c["type"] for c in analysis.build_analysis(self.acct, "food", "last_30_days")["charts"]]
        self.assertIn("line", food)                 # short period, one category -> daily trend
        self.assertIn("horizontal_bar", food)       # two payees
        self.assertNotIn("donut", food)             # never a donut for one category
        overall = [c["type"] for c in analysis.build_analysis(self.acct, "all", "last_30_days")["charts"]]
        self.assertIn("donut", overall)
        travel = analysis.build_analysis(self.acct, "travel", "last_30_days")
        self.assertNotIn("horizontal_bar", [c["type"] for c in travel["charts"]])   # one payee only
        self.assertTrue(all(c["type"] in analysis.CHART_TYPES and c["why"] for c in travel["charts"] + analysis.build_analysis(self.acct)["charts"]))

    def test_empty_state(self):
        spec = analysis.build_analysis(self.acct, "health", "last_7_days")
        self.assertTrue(spec["empty"]); self.assertIn("No health payments", spec["message"])

    def test_indian_digit_grouping(self):
        self.assertEqual(analysis.inr(1234567), "₹12,34,567")
        self.assertEqual(analysis.inr(999), "₹999")
        self.assertEqual(analysis.inr("1500.50"), "₹1,500.50")

    def test_api_and_ownership(self):
        j = "application/json"
        mine = Analysis.objects.create(account=self.acct, params={"category": "food", "period": "last_month"})
        theirs = Analysis.objects.create(account=Account.objects.get(phone="9000000002"), params={})
        self.assertEqual(self.client.get(f"/api/analysis/{mine.id}/").status_code, 401)
        self.client.post("/api/login/", {"phone": "9000000001", "password": "pass1234"}, content_type=j)
        self.assertEqual(self.client.get(f"/api/analysis/{mine.id}/").json()["category"], "food")
        self.assertEqual(self.client.get(f"/api/analysis/{theirs.id}/").status_code, 404)
        self.assertEqual(self.client.get(f"/analysis/{theirs.id}/").status_code, 404)
        self.assertEqual(self.client.get(f"/analysis/{mine.id}/").status_code, 200)
        r = self.client.get("/api/analysis/?category=travel&period=last_3_months").json()
        self.assertEqual((r["category"], r["period"]["key"]), ("travel", "last_3_months"))

    def test_agent_tool_is_bound_to_the_logged_in_account(self):
        ui = []
        tools = {t.name: t for t in agent.build_tools(self.acct, ui)}
        out = async_to_sync(tools["analyze_spending"].ainvoke)({"category": "food", "period": "last_30_days"})
        self.assertIn("Analysis ready", out)
        a = Analysis.objects.get(pk=ui[0]["id"])
        self.assertEqual((a.account_id, a.params["category"]), (self.acct.pk, "food"))

    def test_seed_demo_is_idempotent_and_rich(self):
        call_command("seed_demo", stdout=io.StringIO())
        n = Payment.objects.filter(account__phone="9876543210").count()
        call_command("seed_demo", stdout=io.StringIO())
        self.assertEqual(Payment.objects.filter(account__phone="9876543210").count(), n)
        self.assertTrue(60 <= n <= 110, n)


# ======================================================================= instant answers
@override_settings(VOICE_REQUIRED=False)
class InstantPathTests(TestCase):
    def setUp(self):
        self.acct = make_account()
        Contact.objects.create(account=self.acct, name="Adani Electricity", upi_id="a@bill", kind="biller")
        self.client.post("/api/login/", {"phone": "9000000001", "password": "pass1234"}, content_type="application/json")

    def say(self, text, lang="en-IN"):
        with patch.object(agent, "get_model", side_effect=AssertionError("the AI should not be called")):
            return self.client.post("/api/agent/chat/", {"message": text, "lang": lang}, content_type="application/json").json()

    def test_balance_in_english_and_hindi(self):
        r = self.say("what's my balance?")
        self.assertEqual((r["reply"], r["source"]), ("Your balance is ₹5,000.", "instant"))
        self.assertEqual(self.say("मेरा बैलेंस कितना है", "hi-IN")["reply"], "आपका बैलेंस ₹5,000 है।")

    def test_send_then_yes(self):
        r = self.say("send 300 to Aman")
        self.assertIn("Send ₹300 to Aman? Say yes", r["reply"])
        self.assertFalse(r["pending_payment"]["needs_pin"])
        r = self.say("yes")
        self.assertIn("₹300 sent to Aman", r["reply"])
        self.acct.refresh_from_db(); self.assertEqual(self.acct.balance, Decimal("4700"))

    def test_hindi_send_and_haan(self):
        r = self.say("अमन को 200 रुपये भेजो", "hi-IN")
        self.assertIn("Aman को ₹200 भेजूँ?", r["reply"])
        self.assertIn("हो गया", self.say("हाँ", "hi-IN")["reply"])

    def test_no_cancels(self):
        self.say("send 100 to Aman")
        self.assertEqual(self.say("no")["reply"], "Okay, I've cancelled that payment.")
        self.assertEqual(Payment.objects.filter(status="CANCELLED").count(), 1)

    def test_yes_never_confirms_a_pin_payment(self):
        r = self.say("pay my electricity bill 1200")
        self.assertTrue(r["pending_payment"]["needs_pin"])
        self.assertIn("PIN", r["reply"])
        self.assertIn("PIN", self.say("yes")["reply"])
        self.assertEqual(Payment.objects.filter(status="SUCCESS").count(), 0)

    def test_ambiguous_name_asks_which(self):
        self.assertIn("Rahul Sharma", self.say("send 50 to rahul")["reply"])

    def test_navigation_and_forms(self):
        self.assertEqual(self.say("open settings")["ui"], [{"type": "navigate", "page": "settings"}])
        self.assertEqual(self.say("सेटिंग्स खोलो", "hi-IN")["ui"], [{"type": "navigate", "page": "settings"}])
        self.assertEqual(self.say("go to my profile")["ui"][0]["page"], "profile")
        self.assertEqual(self.say("I want to pay a bill")["ui"][0]["form"], "bills")
        self.assertEqual(self.say("speak in hindi")["ui"][0], {"type": "setting", "setting": "language", "value": "hi-IN"})
        self.assertEqual(self.say("freeze my card")["ui"][0]["value"], "on")

    def test_analysis_by_voice(self):
        r = self.say("analyse my food spending last month")
        self.assertEqual(r["analysis"]["title"], "Food spending, last month")
        self.assertEqual(r["analysis"]["url"], f"/analysis/{r['analysis']['id']}/")
        self.assertEqual(self.say("open it")["ui"][0], {"type": "open_analysis", "url": r["analysis"]["url"]})

    def test_analysis_still_answers_if_the_database_cannot_save_it(self):
        # e.g. a shared database with a different, older analysis table
        from django.db import DatabaseError
        with patch.object(Analysis.objects, "create", side_effect=DatabaseError("column params does not exist")):
            r = self.say("analyse my food spending last month")
        self.assertEqual(r["source"], "instant")
        self.assertEqual(r["analysis"]["url"], "/analysis/?category=food&period=last_month")
        self.assertIsNone(r["analysis"]["id"])

    def test_unclear_or_other_language_goes_to_the_ai(self):
        for text, lang in [("send money to my friend from college", "en-IN"), ("send 300", "en-IN"), ("என் இருப்பு என்ன", "ta-IN")]:
            with patch.object(agent, "get_model", return_value=scripted("AI reply")):
                r = self.client.post("/api/agent/chat/", {"message": text, "lang": lang}, content_type="application/json").json()
            self.assertEqual((r["reply"], r["source"]), ("AI reply", "ai"), text)

    def test_changing_language_starts_a_fresh_conversation(self):
        self.say("what's my balance")
        self.say("मेरा बैलेंस", "hi-IN")
        self.assertEqual(len(self.client.session["chat"]), 2)

    def test_system_prompt_has_live_facts_and_language(self):
        p = agent.build_system_prompt(self.acct, "ta-IN")
        self.assertIn("Tamil", p); self.assertIn("₹5,000", p); self.assertIn("Adani Electricity (biller)", p)


# ======================================================================= sign up, contacts, speech
class SignupTests(TestCase):
    def post(self, body):
        return self.client.post("/api/signup/", body, content_type="application/json")

    def test_signup_creates_a_ready_account_and_logs_in(self):
        r = self.post({"full_name": "Kamla Devi", "phone": "+91 98111 22233", "password": "secret1", "pin": "5829"})
        self.assertEqual(r.status_code, 200, r.json())
        acct = Account.objects.get(phone="9811122233")
        self.assertEqual(acct.balance, services.STARTER_BALANCE)
        self.assertTrue(acct.check_pin("5829"))
        self.assertTrue(acct.contacts.filter(name="Adani Electricity").exists())
        self.assertEqual(self.client.get("/api/me/").json()["name"], "Kamla Devi")
        self.assertEqual(self.client.get("/signup/").status_code, 302)

    def test_signup_validation(self):
        good = {"full_name": "Kamla Devi", "phone": "9811122233", "password": "secret1", "pin": "5829"}
        for change, code in [({"phone": "12345"}, "bad_phone"), ({"password": "abc"}, "bad_password"),
                             ({"pin": "1234"}, "weak_pin"), ({"pin": "12a4"}, "bad_pin"), ({"full_name": ""}, "bad_name")]:
            self.assertEqual(self.post({**good, **change}).json()["code"], code)
        make_account(phone="9811122233")
        self.assertEqual(self.post(good).json()["code"], "phone_taken")

    @override_settings(VOICE_REQUIRED=False)
    def test_add_contact(self):
        make_account()
        j = "application/json"
        self.client.post("/api/login/", {"phone": "9000000001", "password": "pass1234"}, content_type=j)
        self.assertEqual(self.client.post("/api/contacts/", {"name": "Sunita", "upi_id": "bad"}, content_type=j).json()["code"], "bad_upi")
        self.assertTrue(self.client.post("/api/contacts/", {"name": "Sunita", "upi_id": "sunita@okhdfc"}, content_type=j).json()["ok"])
        self.assertEqual(self.client.post("/api/contacts/", {"name": "sunita", "upi_id": "s2@okhdfc"}, content_type=j).json()["code"], "contact_exists")


class SarvamTests(TestCase):
    def setUp(self):
        make_account()
        self.client.post("/api/login/", {"phone": "9000000001", "password": "pass1234"}, content_type="application/json")

    def test_without_a_key_the_browser_takes_over(self):
        with patch.dict("os.environ", {"SARVAM_API_KEY": ""}):
            r = self.client.post("/api/voice/tts/", {"text": "hi"}, content_type="application/json")
            self.assertEqual((r.status_code, r.json()["code"]), (503, "sarvam_unavailable"))
            self.assertFalse(self.client.get("/api/voice/status/").json()["sarvam"])

    def test_stt_and_tts_with_a_key(self):
        from types import SimpleNamespace
        fake = SimpleNamespace(
            speech_to_text=SimpleNamespace(transcribe=lambda **kw: SimpleNamespace(transcript="अमन को 200 भेजो", language_code="hi-IN")),
            text_to_speech=SimpleNamespace(convert=lambda **kw: SimpleNamespace(audios=["QUJD"])))
        with patch.dict("os.environ", {"SARVAM_API_KEY": "test"}), patch.object(views, "_sarvam_client", return_value=fake):
            r = self.client.post("/api/voice/stt/", {"audio": clip(), "lang": "hi-IN"}).json()
            self.assertEqual(r["transcript"], "अमन को 200 भेजो")
            r = self.client.post("/api/voice/tts/", {"text": "नमस्ते", "language_code": "hi-IN"}, content_type="application/json").json()
            self.assertEqual((r["audio_b64"], r["mime"]), ("QUJD", "audio/mpeg"))


# ======================================================================= forgot password (PIN)
J = "application/json"


class ForgotPasswordTests(TestCase):
    """Forgot password = registered mobile number + the account PIN -> new password."""

    def setUp(self):
        self.acct = make_account(phone="9811122233")          # PIN 1234, password pass1234

    def reset(self, pin="1234", password="newpass99", phone="+91 98111 22233"):
        return self.client.post("/api/password/reset/", {"phone": phone, "pin": pin, "password": password}, content_type=J)

    def login(self, password):
        return self.client.post("/api/login/", {"phone": "9811122233", "password": password}, content_type=J)

    def test_reset_with_the_pin(self):
        r = self.reset()
        self.assertTrue(r.json()["ok"], r.json())
        self.assertEqual(r.json()["phone"], "9811122233")
        self.assertEqual(self.login("pass1234").status_code, 400)        # old password no longer works
        self.assertEqual(self.login("newpass99").status_code, 200)
        self.assertTrue(AuditLog.objects.filter(account=self.acct, event="password_reset").exists())

    def test_wrong_pin_and_unknown_number_look_the_same(self):
        wrong = self.reset(pin="9999").json()
        unknown = self.reset(phone="9000000099").json()
        self.assertEqual((wrong["code"], wrong["message"]), (unknown["code"], unknown["message"]))
        self.assertEqual(self.login("newpass99").status_code, 400)

    def test_pin_cannot_be_guessed(self):
        for _ in range(3):
            self.reset(pin="0000")
        r = self.reset(pin="1234")                                   # even the right PIN: locked now
        self.assertEqual((r.status_code, r.json()["code"]), (423, "pin_locked"))
        self.assertEqual(self.login("newpass99").status_code, 400)

    def test_weak_new_password(self):
        self.assertEqual(self.reset(password="abc").json()["code"], "bad_password")

    def test_forgot_page_and_login_link(self):
        self.assertEqual(self.client.get("/forgot-password/").status_code, 200)
        self.assertContains(self.client.get("/login/"), "/forgot-password/")

    def test_login_accepts_plus91_and_spaces(self):
        r = self.client.post("/api/login/", {"phone": "+91 98111 22233", "password": "pass1234"}, content_type=J)
        self.assertEqual(r.status_code, 200)


# ======================================================================= voice check: 15 s numbers
class VoiceWindowTests(TestCase):
    def setUp(self):
        self.acct = make_account()
        self.client.post("/api/login/", {"phone": "9000000001", "password": "pass1234"}, content_type=J)
        VoiceProfile.objects.create(account=self.acct, embedding=[1, 0, 0, 0], samples=3, threshold=0.40)

    def attempt(self, similarity, spoken=None, fresh=True):
        ch = self.client.get("/api/voice/challenge/").json() if fresh else None
        spoken = (ch["digits"] if ch else "") if spoken is None else spoken
        with patch.object(voice, "embed_signal", return_value=vec(similarity)), \
             patch.object(voice, "transcribe_digits", return_value=spoken):
            return self.client.post("/api/voice/verify/", {"audio": clip()}).json()

    def test_numbers_last_fifteen_seconds(self):
        ch = self.client.get("/api/voice/challenge/").json()
        self.assertEqual(ch["seconds"], 15)
        s = self.client.session
        self.assertLessEqual(s["voice_challenge"]["exp"] - time.time(), 15 + views.CHALLENGE_GRACE + 1)
        s["voice_challenge"]["exp"] = time.time() - 1; s.save()          # the 15 s (+ upload time) ran out
        with patch.object(voice, "embed_signal", return_value=vec(0.9)), \
             patch.object(voice, "transcribe_digits", return_value=ch["digits"]):
            r = self.client.post("/api/voice/verify/", {"audio": clip()}).json()
        self.assertEqual(r["code"], "challenge_expired")

    def test_a_new_number_replaces_the_old_one(self):
        old = self.client.get("/api/voice/challenge/").json()["digits"]
        self.client.get("/api/voice/challenge/")
        r = self.attempt(0.9, spoken=old, fresh=False)
        self.assertNotEqual(r["code"], "verified")

    def test_noise_never_locks_the_owner_out(self):
        for _ in range(8):
            self.assertEqual(self.attempt(0.9, spoken="")["code"], "no_digits")
        self.assertEqual(VoiceProfile.objects.get(account=self.acct).failed_attempts, 0)
        self.assertTrue(self.attempt(0.9)["ok"])

    def test_just_under_the_line_is_never_accepted(self):
        for _ in range(3):                                   # retrying can't sneak a voice in any more
            self.assertEqual(self.attempt(0.38)["code"], "voice_mismatch")
        self.assertFalse(self.client.get("/api/voice/status/").json()["verified"])

    def test_old_low_personal_line_is_raised_to_the_floor(self):
        VoiceProfile.objects.filter(account=self.acct).update(threshold=0.32)       # saved by older code
        self.assertEqual(self.attempt(0.36)["code"], "voice_mismatch")
        self.assertTrue(self.attempt(0.45)["ok"])

    def test_a_voice_closer_to_another_account_is_rejected(self):
        # Aniket's voiceprint is on his own account; his clip scores 0.45 against Hunar's
        # print (above the line!) but 0.80 against his own -> it is Aniket, not Hunar.
        aniket = make_account(phone="9000000002")
        VoiceProfile.objects.create(account=aniket, embedding=[0, 1, 0, 0], samples=3, threshold=0.40)
        clip_vec = np.array([0.45, 0.80, 0.0, 0.0])
        ch = self.client.get("/api/voice/challenge/").json()
        with patch.object(voice, "embed_signal", return_value=clip_vec), \
             patch.object(voice, "transcribe_digits", return_value=ch["digits"]):
            r = self.client.post("/api/voice/verify/", {"audio": clip()}).json()
        self.assertEqual(r["code"], "voice_mismatch")
        self.assertTrue(self.attempt(0.9)["ok"])                       # the real owner still gets in

    def test_mostly_noise_is_rejected_and_not_counted(self):
        self.client.get("/api/voice/challenge/")
        r = self.client.post("/api/voice/verify/", {"audio": clip(pad=0.5, noise=0.05, amplitude=0.04)}).json()
        self.assertEqual(r["code"], "audio_noisy")
        self.assertEqual(VoiceProfile.objects.get(account=self.acct).failed_attempts, 0)

    def test_one_voice_cannot_protect_two_accounts(self):
        other = make_account(phone="9000000002")
        VoiceProfile.objects.create(account=other, embedding=[1, 0, 0, 0], samples=3, threshold=0.40)
        VoiceProfile.objects.filter(account=self.acct).delete()
        with patch.object(voice, "embed_signal", side_effect=[vec(0.95), vec(0.9), vec(0.93)]):
            r = self.client.post("/api/voice/enroll/", {"audio": [clip(), clip(), clip()]}).json()
        self.assertEqual(r["code"], "voice_taken")

    def test_speech_to_noise_measure(self):
        rate = voice.SAMPLE_RATE
        tone = 0.2 * np.sin(2 * np.pi * 220 * np.arange(2 * rate) / rate)
        clean = np.concatenate([np.zeros(rate // 2), tone, np.zeros(rate // 2)]) + np.random.default_rng(1).normal(0, 0.001, 3 * rate)
        noise = np.random.default_rng(2).normal(0, 0.05, 3 * rate)
        self.assertGreater(voice.speech_to_noise_db(clean), 30)
        self.assertLess(voice.speech_to_noise_db(noise), voice.MIN_SNR_DB)

    def test_wrong_digits_report_what_was_heard(self):
        r = self.attempt(0.9, spoken="111111")
        self.assertEqual((r["code"], r["heard"]), ("phrase_mismatch", "1 1 1 1 1 1"))

    def test_number_words_are_understood(self):
        self.assertEqual(voice.words_to_digits("four seven two nine one three"), "472913")
        self.assertEqual(voice.words_to_digits("char saat do nau ek teen"), "472913")
        self.assertEqual(voice.words_to_digits("चार सात दो नौ एक तीन"), "472913")
        self.assertEqual(voice.words_to_digits("4 double 7, 2"), "4772")
        self.assertEqual(voice.words_to_digits("४ 7 २"), "472")

    def test_fastest_correct_engine_wins(self):
        slow_wrong = lambda wav: (time.sleep(0.3), "999999")[1]      # noqa: E731
        right = lambda wav: "472913"                                 # noqa: E731
        with patch.object(voice, "stt_engines", return_value=[slow_wrong, right]):
            t = time.time()
            self.assertEqual(voice.heard_digits(b"x", "472913"), "472913")
            self.assertLess(time.time() - t, 0.25)

    def test_one_engine_down_the_other_still_answers(self):
        def down(wav):
            raise voice.VoiceError("stt_unavailable", "down")
        with patch.object(voice, "stt_engines", return_value=[down, lambda wav: "472913"]):
            self.assertEqual(voice.heard_digits(b"x", "472913"), "472913")
        with patch.object(voice, "stt_engines", return_value=[down, down]):
            with self.assertRaises(voice.VoiceError):
                voice.heard_digits(b"x", "472913")
        with patch.object(voice, "stt_engines", return_value=[lambda w: "472914", lambda w: "000000"]):
            self.assertEqual(voice.heard_digits(b"x", "472913"), "472914")      # closest answer


# ======================================================================= real data on every page
@override_settings(VOICE_REQUIRED=False)
class RealDataTests(TestCase):
    def setUp(self):
        self.acct = make_account()
        Contact.objects.create(account=self.acct, name="Swiggy", upi_id="swiggy@upi", kind="merchant")
        Contact.objects.create(account=self.acct, name="EchoMint Gold", upi_id="gold@echomint", kind="gold")
        Contact.objects.create(account=self.acct, name="Jio Prepaid", upi_id="jio@recharge", kind="recharge")
        self.client.post("/api/login/", {"phone": "9000000001", "password": "pass1234"}, content_type=J)

    def pay(self, who, amount, reference=""):
        r = self.client.post("/api/payments/", {"recipient": who, "amount": amount, "reference": reference}, content_type=J).json()
        if r.get("code") == "needs_confirmation":
            r = self.client.post(f"/api/payments/{r['payment_id']}/confirm/", {}, content_type=J).json()
        return r

    @override_settings(GOLD_PRICE_PER_GRAM="15000")
    def test_gold_comes_from_real_purchases(self):
        self.assertEqual(self.client.get("/api/me/").json()["gold"]["grams"], "0.0000")
        self.assertTrue(self.pay("EchoMint Gold", 300)["ok"])
        g = self.client.get("/api/me/").json()["gold"]
        self.assertEqual((g["invested"], g["grams"], g["price_per_gram"]), ("300.00", "0.0200", "15000"))

    def test_card_is_derived_from_the_account_and_freeze_really_blocks(self):
        card = self.client.get("/api/me/").json()["card"]
        self.assertEqual(card["last4"], self.acct.account_number[-4:])
        self.assertRegex(card["valid_till"], r"^\d\d/\d\d$")
        self.assertTrue(self.client.post("/api/card/freeze/", {"frozen": True}, content_type=J).json()["frozen"])
        self.assertEqual(self.pay("Swiggy", 200)["code"], "card_frozen")      # shop payment blocked
        self.assertTrue(self.pay("Aman", 200)["ok"])                         # people still fine
        self.client.post("/api/card/freeze/", {"frozen": False}, content_type=J)
        self.assertTrue(self.pay("Swiggy", 200)["ok"])
        events = set(AuditLog.objects.values_list("event", flat=True))
        self.assertTrue({"card_frozen", "card_unfrozen"} <= events)

    def test_recharge_number_is_saved_with_the_payment(self):
        self.assertEqual(self.pay("Jio Prepaid", 199, "12345")["code"], "bad_reference")
        self.assertTrue(self.pay("Jio Prepaid", 199, "9811122233")["ok"])
        row = self.client.get("/api/history/").json()["payments"][0]
        self.assertEqual(row["reference"], "9811122233")


class SmallPaymentSafetyTests(TestCase):
    def setUp(self):
        self.acct = make_account(balance="20000")

    def send_yes(self, amount):
        r = services.initiate_payment(self.acct, "Aman", amount)
        if r["code"] == "needs_confirmation":
            return services.confirm_payment(self.acct, r["payment_id"])
        return r

    def test_many_small_yes_payments_need_the_pin_after_the_hourly_cap(self):
        for _ in range(3):
            self.assertEqual(self.send_yes(490)["code"], "success")       # 1,470 by voice alone
        r = services.initiate_payment(self.acct, "Aman", 100)              # would pass 1,500
        self.assertEqual(r["code"], "needs_pin")
        self.assertFalse(services.confirm_payment(self.acct, r["payment_id"])["ok"])   # "yes" is not enough
        self.assertTrue(services.confirm_payment(self.acct, r["payment_id"], pin="1234")["ok"])

    def test_a_lone_ji_or_ha_does_not_send_money(self):
        session = {}
        intents.handle(self.acct, "send 200 to Aman", "en-IN", session)
        for word in ("ji", "ha", "जी"):
            out = intents.handle(self.acct, word, "hi-IN", session)
            self.assertFalse(out and "success" in str(out.get("reply", "")).lower())
        self.assertFalse(Payment.objects.filter(status=Payment.Status.SUCCESS).exists())
        intents.handle(self.acct, "haan", "hi-IN", session)
        self.assertTrue(Payment.objects.filter(status=Payment.Status.SUCCESS).exists())



class LiveDemoTests(TestCase):
    """What the judges will see: owner in, friend out, lock, and no Wi-Fi surprises."""

    def setUp(self):
        self.acct = make_account()
        self.client.post("/api/login/", {"phone": "9000000001", "password": "pass1234"}, content_type=J)
        VoiceProfile.objects.create(account=self.acct, embedding=[1, 0, 0, 0], samples=3, threshold=0.42)
        friend = make_account(phone="9000000002")
        VoiceProfile.objects.create(account=friend, embedding=[0, 1, 0, 0], samples=3, threshold=0.42)

    def attempt(self, vector, stt=None):
        ch = self.client.get("/api/voice/challenge/").json()
        side = {"side_effect": stt} if stt else {"return_value": ch["digits"]}
        with patch.object(voice, "embed_signal", return_value=np.array(vector)), \
             patch.object(voice, "transcribe_digits", **side):
            return self.client.post("/api/voice/verify/", {"audio": clip()})

    def test_owner_in_friend_out_then_lock_and_repeat(self):
        self.assertTrue(self.attempt([0.8, 0.1, 0, 0]).json()["ok"])                  # owner
        self.assertTrue(self.client.get("/api/voice/status/").json()["verified"])
        self.assertTrue(self.client.post("/api/voice/lock/", content_type=J).json()["ok"])
        self.assertFalse(self.client.get("/api/voice/status/").json()["verified"])
        self.assertEqual(self.attempt([0.1, 0.8, 0, 0]).json()["code"], "voice_mismatch")   # friend
        self.assertTrue(self.attempt([0.8, 0.1, 0, 0]).json()["ok"])                  # owner again

    @override_settings(VOICE_SHOW_SCORES=True)
    def test_scores_for_the_judges(self):
        sc = self.attempt([0.8, 0.1, 0, 0]).json()["scores"]
        self.assertEqual(sc["needed"], 42); self.assertGreater(sc["you"], sc["needed"]); self.assertLess(sc["other"], 20)

    def test_scores_hidden_by_default(self):
        self.assertNotIn("scores", self.attempt([0.8, 0.1, 0, 0]).json())

    def test_wifi_down_clear_owner_still_gets_in_but_borderline_does_not(self):
        down = voice.VoiceError("stt_unavailable", "down")
        self.assertTrue(self.attempt([0.9, 0.05, 0, 0], stt=down).json()["ok"])        # clear match
        self.assertTrue(AuditLog.objects.filter(event="voice_digits_skipped").exists())
        self.client.post("/api/voice/lock/", content_type=J)
        r = self.attempt([0.46, 0.3, 0.8, 0], stt=down)                                  # just over the line
        self.assertEqual((r.status_code, r.json()["code"]), (503, "stt_unavailable"))

    @override_settings(VOICE_ALLOW_NO_STT=False)
    def test_wifi_fallback_can_be_switched_off(self):
        r = self.attempt([0.9, 0.05, 0, 0], stt=voice.VoiceError("stt_unavailable", "down"))
        self.assertEqual(r.status_code, 503)


@override_settings(VOICE_REQUIRED=False)
class ContactManagementTests(TestCase):
    def setUp(self):
        self.acct = make_account()
        self.client.post("/api/login/", {"phone": "9000000001", "password": "pass1234"}, content_type=J)

    def cid(self, name):
        return next(c["id"] for c in self.client.get("/api/me/").json()["contacts"] if c["name"] == name)

    def test_add_then_remove_and_history_stays(self):
        self.assertTrue(self.client.post("/api/contacts/", {"name": "Sunita", "upi_id": "sunita@okhdfc"}, content_type=J).json()["ok"])
        r = self.client.post("/api/payments/", {"recipient": "Sunita", "amount": 100}, content_type=J).json()
        self.client.post(f"/api/payments/{r['payment_id']}/confirm/", {}, content_type=J)
        r = self.client.post(f"/api/contacts/{self.cid('Sunita')}/remove/", content_type=J).json()
        self.assertEqual(r["code"], "contact_removed")
        self.assertNotIn("Sunita", [c["name"] for c in self.client.get("/api/me/").json()["contacts"]])
        self.assertEqual(self.client.get("/api/history/").json()["payments"][0]["recipient"], "Sunita")   # history kept
        self.assertEqual(self.client.post("/api/payments/", {"recipient": "Sunita", "amount": 50}, content_type=J).json()["code"],
                         "recipient_not_found")                                                           # can't pay any more

    def test_pending_payment_to_removed_contact_is_cancelled(self):
        r = self.client.post("/api/payments/", {"recipient": "Aman", "amount": 100}, content_type=J).json()
        self.client.post(f"/api/contacts/{self.cid('Aman')}/remove/", content_type=J)
        self.assertEqual(Payment.objects.get(pk=r["payment_id"]).status, Payment.Status.CANCELLED)

    def test_builtin_payees_and_other_peoples_contacts_are_protected(self):
        Contact.objects.create(account=self.acct, name="Adani Electricity", upi_id="a@billpay", kind="biller")
        self.assertEqual(self.client.post(f"/api/contacts/{self.cid('Adani Electricity')}/remove/", content_type=J).json()["code"], "contact_builtin")
        other = make_account(phone="9000000002")
        theirs = other.contacts.get(name="Aman").id
        self.assertEqual(self.client.post(f"/api/contacts/{theirs}/remove/", content_type=J).json()["code"], "contact_not_found")
        self.assertTrue(Contact.objects.filter(pk=theirs).exists())

    def test_removing_needs_the_voice_check(self):
        with override_settings(VOICE_REQUIRED=True):
            self.assertEqual(self.client.post(f"/api/contacts/{self.cid('Aman')}/remove/", content_type=J).status_code, 403)
