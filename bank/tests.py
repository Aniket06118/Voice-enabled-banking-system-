from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
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


class WebTests(TestCase):
    def setUp(self):
        self.acct = make_account()

    def test_home_page_renders(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "EchoMint")

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
