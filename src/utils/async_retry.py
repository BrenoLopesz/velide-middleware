import asyncio
import functools
import logging
from typing import Any, Callable, Optional, TypeVar, Union, Awaitable

import httpx

from utils.exception_translator import get_friendly_error_msg

T = TypeVar('T')

# Type for the exception callback - can be sync or async
ExceptionCallback = Callable[[Exception, int, tuple, dict], Union[Optional[T], Awaitable[Optional[T]]]]


def async_retry(
    operation_desc: str,
    max_retries: int = 3,
    initial_delay: float = 1.0,
    backoff_factor: float = 2.0,
    exceptions: tuple = (httpx.RequestError, httpx.TimeoutException),
    on_exception: Optional[ExceptionCallback] = None,
):
    """
    Decorator with user-friendly logging and optional exception callback.

    Args:
        operation_desc: Human-readable description for logging
        max_retries: Maximum number of retry attempts
        initial_delay: Initial delay between retries (seconds)
        backoff_factor: Multiplier for exponential backoff
        exceptions: Tuple of exceptions to catch and retry
        on_exception: Optional callback function called on each exception.
                     Receives (exception, attempt_number, args, kwargs).
                     Can be a sync or async function.
                     If callback returns a non-None value, that value is returned
                     immediately (skipping retry). If callback returns None,
                     normal retry logic continues.

    Example:
        @async_retry(operation_desc="send delivery")
        async def add_delivery(order):
            ...

    Example with callback:
        async def handle_timeout(exc, attempt, args, kwargs):
            if isinstance(exc, httpx.TimeoutException):
                order = args[0] if args else None
                # Try to reconcile
                result = await check_if_already_created(order)
                if result:
                    return result  # Skip retry, return existing
            return None  # Continue with retry

        @async_retry(
            operation_desc="send delivery",
            on_exception=handle_timeout
        )
        async def add_delivery(order): ...
    """

    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            delay = initial_delay
            last_exception: Optional[Exception] = None

            # Setup Logger
            logger = logging.getLogger(func.__module__)
            if args and hasattr(args[0], "logger"):
                logger = args[0].logger

            for attempt in range(1, max_retries + 1):
                try:
                    return await func(*args, **kwargs)
                except exceptions as e:
                    last_exception = e

                    # --- EXCEPTION CALLBACK HOOK ---
                    if on_exception is not None:
                        # Pass args and kwargs so the callback can access context
                        result = on_exception(e, attempt, args, kwargs)
                        # Handle both sync and async callbacks
                        if asyncio.iscoroutine(result):
                            callback_result = await result
                        else:
                            callback_result = result
                        if callback_result is not None:
                            logger.debug(
                                f"Exception handler returned result for {operation_desc}. "
                                "Skipping retry."
                            )
                            return callback_result
                    # --- END CALLBACK HOOK ---

                    # Check if we should stop
                    if attempt == max_retries:
                        break

                    # Translate and log the error
                    friendly_error = get_friendly_error_msg(e)
                    logger.warning(
                        f"Tentativa {attempt}/{max_retries} "
                        f"de {operation_desc} falhou: "
                        f"{friendly_error}. Aguardando {delay}s..."
                    )

                    await asyncio.sleep(delay)
                    delay *= backoff_factor

            # Final failure log before raising
            logger.error(f"Todas as tentativas de {operation_desc} falharam.")
            if last_exception is not None:
                raise last_exception
            # This should never happen, but satisfies type checker
            raise RuntimeError(f"Erro inesperado ao reiniciar operação de {operation_desc}.")

        return wrapper

    return decorator
