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
        with patch.object(agent, "get_model", return_value=scripted(
                ("start_payment", {"recipient_name": "Aman", "amount": 900}), "Enter your PIN.")):
            r = self.client.post("/api/agent/chat/", {"message": "send 900 to aman"}, content_type=j).json()
        self.assertEqual(r["reply"], "Enter your PIN.")
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


def wav_bytes(seconds=3.0, amplitude=0.2, rate=16000):
    t = np.arange(int(seconds * rate)) / rate
    data = (amplitude * np.sin(2 * np.pi * 220 * t) * 32767).astype("<i2")
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
        samples = voice.decode_wav(wav_bytes(seconds=2, rate=48000))
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
        self.assertEqual(len(self.client.get("/api/voice/challenge/").json()["digits"]), 6)
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
        self.assertEqual(VoiceProfile.objects.get(account=self.acct).threshold, 0.42)

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
        self.assertTrue(voice.digits_close("123457", "123456"))
        self.assertFalse(voice.digits_close("123477", "123456"))
        self.assertFalse(voice.digits_close("12345", "123456"))

    def test_one_wrong_digit_is_forgiven_only_when_voice_is_clearly_the_owner(self):
        ch = self.client.get("/api/voice/challenge/").json()["digits"]
        near = ch[:-1] + str((int(ch[-1]) + 1) % 10)
        with patch.object(voice, "embed_signal", return_value=vec(0.80)), \
             patch.object(voice, "transcribe_digits", return_value=near):
            self.assertTrue(self.client.post("/api/voice/verify/", {"audio": clip()}).json()["ok"])
        self.fresh()
        ch = self.client.get("/api/voice/challenge/").json()["digits"]
        near = ch[:-1] + str((int(ch[-1]) + 1) % 10)
        with patch.object(voice, "embed_signal", return_value=vec(0.45)), \
             patch.object(voice, "transcribe_digits", return_value=near):                  # borderline voice
            self.assertEqual(self.client.post("/api/voice/verify/", {"audio": clip()}).json()["code"], "phrase_mismatch")

    def test_totally_wrong_digits_always_fail(self):
        r, _ = self.verify(0.95, spoken="000000")
        self.assertEqual(r.json()["code"], "phrase_mismatch")

    # --- learning ------------------------------------------------------
    def test_voiceprint_learns_from_clean_logins_only(self):
        before = VoiceProfile.objects.get(account=self.acct)
        self.verify(0.80)
        after = VoiceProfile.objects.get(account=self.acct)
        self.assertEqual(after.samples, before.samples + 1)
        self.assertNotEqual(after.embedding, before.embedding)
        self.fresh(); self.verify(0.42)                                  # passes, but not cleanly enough to learn
        self.assertEqual(VoiceProfile.objects.get(account=self.acct).samples, after.samples)

    # --- not getting stuck ---------------------------------------------
    def test_speech_service_outage_is_not_the_users_fault(self):
        ch = self.client.get("/api/voice/challenge/").json()
        err = voice.VoiceError("stt_unavailable", "down")
        with patch.object(voice, "embed_signal", return_value=vec(0.9)), \
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
        self.assertIn({"name": "Adani Electricity", "upi_id": "a@bill", "kind": "biller"}, me["contacts"])

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
