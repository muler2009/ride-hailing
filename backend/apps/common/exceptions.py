from rest_framework.views import exception_handler as drf_exception_handler


def api_exception_handler(exc, context):
    """
    Wraps every DRF error response in a single, consistent envelope:

        {
          "error": {
            "code": "validation_error",
            "message": "Human readable summary.",
            "details": { ... field-level or extra info ... }
          }
        }

    This is a Phase-1 API convention: every endpoint in the platform
    returns errors in this shape, so clients only ever need to parse one
    structure regardless of which domain module raised the error.
    """
    response = drf_exception_handler(exc, context)

    if response is None:
        return response

    default_detail = getattr(exc, "default_detail", "An error occurred.")
    code = getattr(exc, "default_code", "error")

    details = response.data
    if isinstance(details, dict) and set(details.keys()) == {"detail"}:
        message = str(details["detail"])
        details = None
    elif isinstance(details, list):
        message = str(default_detail)
    else:
        message = str(default_detail)

    response.data = {
        "error": {
            "code": code,
            "message": message,
            "details": details,
        }
    }
    return response
