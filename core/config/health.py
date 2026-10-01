from django.db import OperationalError, connection
from django.http import JsonResponse


def health(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    except OperationalError:
        return JsonResponse({"status": "error", "db": "down"}, status=503)
    return JsonResponse({"status": "ok", "db": "up"})
