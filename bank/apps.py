import sys
import threading

from django.apps import AppConfig


class BankConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "bank"

    def ready(self):
        # Load the voice model as soon as the server starts, so the FIRST voice check
        # on stage is as fast as the rest (loading it takes a few seconds).
        if "runserver" in sys.argv:
            from django.conf import settings
            if settings.VOICE_REQUIRED:
                from . import voice
                threading.Thread(target=voice.warm_up, daemon=True).start()
