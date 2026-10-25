class BharatGateError(Exception):
    """Base exception for all BharatGate errors."""


class BharatCodeConfigError(BharatGateError):
    """Raised when BHARATCODE_API_KEY or required configuration is missing or invalid."""


class BharatCodeAPIError(BharatGateError):
    """Raised when the BharatCode API returns an HTTP error or exhausts retries."""

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        request_id: str | None = None,
        response_body: str | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.request_id = request_id
        self.response_body = response_body
        self.retryable = retryable


class TruncatedToolCallError(BharatGateError):
    """Raised when max_output_tokens truncates a response containing a function_call."""

    def __init__(
        self,
        message: str,
        request_id: str | None = None,
        reason: str | None = "max_output_tokens",
        partial_text: str = "",
    ) -> None:
        super().__init__(message)
        self.request_id = request_id
        self.reason = reason
        self.partial_text = partial_text


class SchemaRepairExhaustedError(BharatGateError):
    """Raised when structured JSON output fails Pydantic validation after self-repair."""
