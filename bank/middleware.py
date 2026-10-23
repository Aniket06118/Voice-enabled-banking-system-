import logging

from django.db.utils import InterfaceError, OperationalError
from django.http import JsonResponse

log = logging.getLogger(__name__)


class ApiErrorMiddleware:
    """If anything unexpected happens inside /api/ (for example the Wi-Fi drops
    and the database can't be reached), answer with a calm JSON message that the
    website can show and speak, instead of an HTML error page.

    Payments run inside a database transaction, so a failure halfway through
    rolls everything back: "your money is safe" is true.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_exception(self, request, exception):
        if not request.path.startswith("/api/"):
            return None
        log.exception("API error on %s", request.path)
        if isinstance(exception, (OperationalError, InterfaceError)):
            return JsonResponse({
                "ok": False, "code": "database_unavailable",
                "message": "I can't reach the bank right now. Please check the internet "
                           "connection and try again. Your money is safe.",
            }, status=503)
        return JsonResponse({
            "ok": False, "code": "server_error",
            "message": "Something went wrong on our side. Your money is safe. Please try again.",
        }, status=500)
