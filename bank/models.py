import re
import uuid
from decimal import Decimal

from django.conf import settings
from django.contrib.auth.hashers import check_password, make_password
from django.db import models
from django.db.models import Q


class Account(models.Model):
    """A bank customer. Login is the Django User (username = phone number)."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="account"
    )
    full_name = models.CharField(max_length=120)
    phone = models.CharField(max_length=15, unique=True)
    account_number = models.CharField(max_length=20, unique=True)
    bank_name = models.CharField(max_length=60, default="Lotus Bank")
    currency = models.CharField(max_length=3, default="INR")
    balance = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))
    daily_limit = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("25000")
    )

    # 4-digit UPI-style PIN (stored hashed, never in plain text)
    pin_hash = models.CharField(max_length=128, blank=True)
    pin_failed_attempts = models.PositiveSmallIntegerField(default=0)
    pin_locked_until = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(balance__gte=0), name="account_balance_non_negative"
            )
        ]

    def __str__(self):
        return f"{self.full_name} ({self.phone})"

    def set_pin(self, raw_pin: str):
        if not re.fullmatch(r"\d{4}", str(raw_pin)):
            raise ValueError("PIN must be exactly 4 digits.")
        self.pin_hash = make_password(str(raw_pin))

    def check_pin(self, raw_pin) -> bool:
        if not self.pin_hash or raw_pin is None:
            return False
        return check_password(str(raw_pin), self.pin_hash)


class Contact(models.Model):
    account = models.ForeignKey(
        Account, on_delete=models.CASCADE, related_name="contacts"
    )
    name = models.CharField(max_length=120)
    upi_id = models.CharField(max_length=120)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["account", "name"], name="unique_contact_name_per_account"
            )
        ]

    def __str__(self):
        return f"{self.name} <{self.upi_id}>"


class Payment(models.Model):
    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        SUCCESS = "SUCCESS", "Success"
        FAILED = "FAILED", "Failed"
        CANCELLED = "CANCELLED", "Cancelled"

    class Auth(models.TextChoices):
        VOICE_CONFIRM = "VOICE_CONFIRM", "Voice confirmation (under 500)"
        PIN = "PIN", "4-digit PIN (500 and above)"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    account = models.ForeignKey(
        Account, on_delete=models.CASCADE, related_name="payments"
    )
    contact = models.ForeignKey(
        Contact, null=True, blank=True, on_delete=models.SET_NULL, related_name="payments"
    )
    recipient_name = models.CharField(max_length=120)
    upi_id = models.CharField(max_length=120, blank=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.PENDING
    )
    auth_required = models.CharField(max_length=15, choices=Auth.choices)
    auth_used = models.CharField(max_length=15, choices=Auth.choices, blank=True)
    # Same key sent twice => same payment, never charged twice.
    idempotency_key = models.CharField(max_length=64, null=True, blank=True)
    failure_reason = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(
                condition=Q(amount__gt=0), name="payment_amount_positive"
            ),
            models.UniqueConstraint(
                fields=["account", "idempotency_key"],
                condition=Q(idempotency_key__isnull=False),
                name="unique_idempotency_key_per_account",
            ),
        ]
        indexes = [models.Index(fields=["account", "-created_at"])]

    def __str__(self):
        return f"{self.recipient_name} {self.amount} [{self.status}]"


class AuditLog(models.Model):
    """Every important event (payment started, wrong PIN, lockout...)."""

    account = models.ForeignKey(
        Account, null=True, on_delete=models.SET_NULL, related_name="audit_logs"
    )
    event = models.CharField(max_length=50)
    detail = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.created_at:%d %b %H:%M} {self.event}"
