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
    path("payments/", views.start_payment),
    path("payments/<uuid:payment_id>/confirm/", views.confirm_payment),
    path("payments/<uuid:payment_id>/cancel/", views.cancel_payment),
    path("agent/chat/", views.agent_chat),
    path("agent/reset/", views.agent_reset),
]
