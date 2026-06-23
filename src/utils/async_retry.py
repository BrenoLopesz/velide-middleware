import asyncio
import functools
import logging
import sys
from typing import Any, Callable, Optional, TypeVar, Union, Awaitable, cast, Tuple, Dict, Type

# Python 3.8 compatibility layer for modern type features
if sys.version_info >= (3, 10):
    from typing import ParamSpec
else:
    from typing_extensions import ParamSpec

from collections.abc import Awaitable as AbcAwaitable
import httpx

from utils.exception_translator import get_friendly_error_msg

T = TypeVar('T')
P = ParamSpec('P')

# Type alias for the exception callback compatible with Python 3.8
ExceptionCallback = Callable[[Exception, int, Tuple[Any, ...], Dict[str, Any]], Union[Optional[T], Awaitable[Optional[T]]]]


async def execute_with_retry(
    coro_fn: Callable[..., Awaitable[T]],
    *args: Any,
    operation_desc: str = "executar operação",
    max_retries: int = 3,
    initial_delay: float = 1.0,
    backoff_factor: float = 2.0,
    exceptions: Tuple[Type[Exception], ...] = (httpx.RequestError, httpx.TimeoutException),
    on_exception: Optional[ExceptionCallback[T]] = None,
    logger: Optional[Any] = None,
    **kwargs: Any
) -> T:
    """Executes an async coroutine function with exponential backoff retry logic.

    Args:
        coro_fn: The asynchronous function/method to execute.
        *args: Positional arguments to pass to `coro_fn`.
        operation_desc: A human-readable description of the operation.
        max_retries: Total number of execution attempts allowed before failing.
        initial_delay: Base sleep duration in seconds between attempts.
        backoff_factor: The multiplier applied to the delay after each failure.
        exceptions: A tuple of exception classes that should trigger a retry.
        on_exception: An optional sync/async callback function called on exception.
        logger: An optional logger instance.
        **kwargs: Keyword arguments to pass to `coro_fn`.

    Returns:
        The evaluated result of type `T` from `coro_fn` or the `on_exception` hook.
    """
    delay = initial_delay
    last_exception: Optional[Exception] = None

    if logger is None:
        logger = logging.getLogger(__name__)

    for attempt in range(1, max_retries + 1):
        try:
            return await coro_fn(*args, **kwargs)
        except exceptions as e:
            last_exception = e

            # --- EXCEPTION CALLBACK HOOK ---
            if on_exception is not None:
                result = on_exception(e, attempt, args, kwargs)
                
                if isinstance(result, AbcAwaitable):
                    resolved_result = await result
                else:
                    resolved_result = result
                
                callback_result = cast(Optional[T], resolved_result)

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
                f"Tentativa {attempt}/{max_retries} de {operation_desc} falhou: "
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


def async_retry(
    operation_desc: str,
    max_retries: int = 3,
    initial_delay: float = 1.0,
    backoff_factor: float = 2.0,
    exceptions: Tuple[Type[Exception], ...] = (httpx.RequestError, httpx.TimeoutException),
    on_exception: Optional[ExceptionCallback[Any]] = None,
) -> Callable[[Callable[P, Awaitable[T]]], Callable[P, Awaitable[T]]]:
    """A decorator wrapper to easily apply exponential retry logic to async functions."""
    def decorator(func: Callable[P, Awaitable[T]]) -> Callable[P, Awaitable[T]]:
        @functools.wraps(func)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
            logger = logging.getLogger(func.__module__)
            
            if args:
                logger = getattr(args[0], "logger", logger)

            return await execute_with_retry(
                func,
                *args,
                operation_desc=operation_desc,
                max_retries=max_retries,
                initial_delay=initial_delay,
                backoff_factor=backoff_factor,
                exceptions=exceptions,
                on_exception=on_exception,
                logger=logger,
                **kwargs
            )

        return wrapper

    return decorator