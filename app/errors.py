from fastapi.responses import JSONResponse


class ApiError(Exception):
    """Raised anywhere in a request; rendered as {"error": message} with the given status."""

    def __init__(self, status_code: int, message: str, headers: dict[str, str] | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.headers = headers or {}


def error_response(status_code: int, message: str, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status_code, headers=headers)
