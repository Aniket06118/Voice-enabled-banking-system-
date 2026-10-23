from django.contrib import admin

from .models import Account, AuditLog, Contact, Payment


class ContactInline(admin.TabularInline):
    model = Contact
    extra = 0


@admin.register(Account)
class AccountAdmin(admin.ModelAdmin):
    list_display = ("full_name", "phone", "account_number", "balance", "daily_limit")
    search_fields = ("full_name", "phone", "account_number")
    exclude = ("pin_hash",)
    inlines = [ContactInline]


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    # Handy to keep open on a second screen during the demo.
    list_display = (
        "created_at", "account", "recipient_name", "amount",
        "status", "auth_required", "auth_used",
    )
    list_filter = ("status", "auth_required")
    search_fields = ("recipient_name", "account__full_name")
    readonly_fields = ("id", "created_at", "completed_at")


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "account", "event", "detail")
    list_filter = ("event",)
