from django.urls import path

from . import views

urlpatterns = [
    path("health/", views.health),
    path("csrf/", views.csrf),
    path("login/", views.login_view),
    path("logout/", views.logout_view),
    path("me/", views.me),
    path("history/", views.history),
    path("summary/", views.summary),
    path("notifications/", views.notifications),
    path("pin/change/", views.change_pin),
    path("settings/daily-limit/", views.daily_limit),
    path("payments/", views.start_payment),
    path("payments/<uuid:payment_id>/confirm/", views.confirm_payment),
    path("payments/<uuid:payment_id>/cancel/", views.cancel_payment),
    path("voice/status/", views.voice_status),
    path("voice/challenge/", views.voice_challenge),
    path("voice/enroll/", views.voice_enroll),
    path("voice/verify/", views.voice_verify),
    path("voice/fallback/", views.voice_fallback),
    path("agent/chat/", views.agent_chat),
    path("agent/reset/", views.agent_reset),
]
