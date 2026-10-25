import random
import threading
import time
from typing import Callable
import httpx

from bharat_gate.core.exceptions import BharatCodeAPIError

RETRYABLE_STATUS_CODES = {429, 503}
NON_RETRYABLE_STATUS_CODES = {400, 401, 403, 404, 422}


class RequestQueue:
    """Thread-safe request queue enforcing concurrency limits and retry backoff with jitter."""

    def __init__(
        self,
        max_concurrent: int = 2,
        max_retries: int = 4,
        base_delay: float = 2.0,
        sleep_fn: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self.max_concurrent = max_concurrent
        self.max_retries = max_retries
        self.base_delay = base_delay
        self._sleep_fn = sleep_fn
        self._rng = rng or random.Random()
        self._semaphore = threading.BoundedSemaphore(max_concurrent)

    def execute_with_retry(
        self,
        operation: Callable[[], httpx.Response],
        on_retry: Callable[[int, float, int | None, str | None], None] | None = None,
    ) -> tuple[httpx.Response, int]:
        """Executes an HTTP operation with bounded concurrency and exponential backoff on retryable errors.

        Returns (response, retry_attempt_count).
        """
        with self._semaphore:
            for attempt in range(self.max_retries + 1):
                try:
                    response = operation()
                except (httpx.TimeoutException, httpx.NetworkError) as exc:
                    if attempt == self.max_retries:
                        raise BharatCodeAPIError(
                            f"Network or timeout error exhausted {self.max_retries} retries: {exc}",
                            status_code=None,
                            request_id=None,
                            response_body=None,
                            retryable=True,
                        ) from exc
                    wait_seconds = self.base_delay * (2 ** attempt) + self._rng.uniform(0.1, 0.75)
                    if on_retry:
                        on_retry(attempt + 1, wait_seconds, None, None)
                    self._sleep_fn(wait_seconds)
                    continue

                request_id = response.headers.get("x-request-id")
                if response.status_code == 200:
                    return (response, attempt)

                if response.status_code in NON_RETRYABLE_STATUS_CODES:
                    raise BharatCodeAPIError(
                        f"Non-retryable client error: HTTP {response.status_code}",
                        status_code=response.status_code,
                        request_id=request_id,
                        response_body=response.text,
                        retryable=False,
                    )

                if response.status_code in RETRYABLE_STATUS_CODES or 500 <= response.status_code < 600:
                    if attempt == self.max_retries:
                        raise BharatCodeAPIError(
                            f"HTTP {response.status_code} exhausted {self.max_retries} retries",
                            status_code=response.status_code,
                            request_id=request_id,
                            response_body=response.text,
                            retryable=True,
                        )
                    wait_seconds = self.base_delay * (2 ** attempt) + self._rng.uniform(0.1, 0.75)
                    if on_retry:
                        on_retry(attempt + 1, wait_seconds, response.status_code, request_id)
                    self._sleep_fn(wait_seconds)
                    continue

                raise BharatCodeAPIError(
                    f"Unexpected HTTP {response.status_code}",
                    status_code=response.status_code,
                    request_id=request_id,
                    response_body=response.text,
                    retryable=False,
                )

            raise BharatCodeAPIError("Max retries exhausted", retryable=True)
