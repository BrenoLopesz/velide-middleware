import asyncio
import functools
import logging
from typing import Any, Callable, Optional, TypeVar, Union, Awaitable, ParamSpec, cast
from collections.abc import Awaitable as AbcAwaitable

import httpx

from utils.exception_translator import get_friendly_error_msg

T = TypeVar('T')
P = ParamSpec('P')

# Type alias for the exception callback - can be a sync or async function
ExceptionCallback = Callable[[Exception, int, tuple[Any, ...], dict[str, Any]], Union[Optional[T], Awaitable[Optional[T]]]]


async def execute_with_retry(
    coro_fn: Callable[..., Awaitable[T]],
    *args: Any,
    operation_desc: str = "executar operação",
    max_retries: int = 3,
    initial_delay: float = 1.0,
    backoff_factor: float = 2.0,
    exceptions: tuple[type[Exception], ...] = (httpx.RequestError, httpx.TimeoutException),
    on_exception: Optional[ExceptionCallback[T]] = None,
    logger: Optional[Any] = None,
    **kwargs: Any
) -> T:
    """Executes an async coroutine function with exponential backoff retry logic.

    Args:
        coro_fn: The asynchronous function/method to execute.
        *args: Positional arguments to pass to `coro_fn`.
        operation_desc: A human-readable description of the operation, used
            primarily for logging failures and attempts.
        max_retries: Total number of execution attempts allowed before failing.
        initial_delay: Base sleep duration in seconds between the first and
            second attempt.
        backoff_factor: The multiplier applied to the delay after each sequential
            failure (exponential backoff).
        exceptions: A tuple of exception classes that should trigger a retry attempt
            when caught.
        on_exception: An optional callback function invoked immediately when an
            eligible exception is caught. Receives `(exception, attempt_number,
            args, kwargs)`. Can be either a synchronous or asynchronous function.
            If it returns a non-None value, that value is returned immediately,
            bypassing any further retry attempts.
        logger: An optional logger instance. Defaults to the module-level logger
            if None is provided.
        **kwargs: Keyword arguments to pass to `coro_fn`.

    Returns:
        The evaluated result of type `T` from `coro_fn` or the `on_exception` hook.

    Raises:
        Exception: Re-raises the final encountered exception if all retry attempts
            are exhausted.
        RuntimeError: Raised if the retry loop terminates unexpectedly without an
            explicit tracked exception state.
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
                
                # Dynamically resolve both sync and async callback boundaries
                if isinstance(result, AbcAwaitable):
                    resolved_result = await result
                else:
                    resolved_result = result
                
                # Explicitly cast to purge object* inference pollution for Pyright
                callback_result = cast(Optional[T], resolved_result)

                if callback_result is not None:
                    logger.debug(
                        f"Exception handler returned result for {operation_desc}. "
                        "Skipping retry."
                    )
                    return callback_result
            # --- END CALLBACK HOOK ---

            if attempt == max_retries:
                break

            friendly_error = get_friendly_error_msg(e)
            logger.warning(
                f"Tentativa {attempt}/{max_retries} de {operation_desc} falhou: "
                f"{friendly_error}. Aguardando {delay}s..."
            )

            await asyncio.sleep(delay)
            delay *= backoff_factor

    logger.error(f"Todas as tentativas de {operation_desc} falharam.")
    if last_exception is not None:
        raise last_exception
        
    raise RuntimeError(f"Erro inesperado ao reiniciar operação de {operation_desc}.")


def async_retry(
    operation_desc: str,
    max_retries: int = 3,
    initial_delay: float = 1.0,
    backoff_factor: float = 2.0,
    exceptions: tuple[type[Exception], ...] = (httpx.RequestError, httpx.TimeoutException),
    on_exception: Optional[ExceptionCallback[Any]] = None,
) -> Callable[[Callable[P, Awaitable[T]]], Callable[P, Awaitable[T]]]:
    """A decorator wrapper to easily apply exponential retry logic to async functions.

    This acts as a seamless wrapper around `execute_with_retry`. It safely forwards
    and isolates parameter signatures and return types using ParamSpec, preventing
    wrapped methods from losing their type contracts in static checkers.

    Args:
        operation_desc: A human-readable description of the operation for logs.
        max_retries: Total number of execution attempts allowed before failing.
        initial_delay: Base sleep duration in seconds between execution attempts.
        backoff_factor: Exponential backoff multiplier applied to subsequent delays.
        exceptions: A tuple of exception classes that should trigger a retry attempt.
        on_exception: An optional sync or async callback function executed upon catching
            an eligible exception. If it returns a value, the retry loop is broken
            and that value is returned.

    Returns:
        A decorator callable that takes an async function and wraps it with retry mechanics
        while fully preserving its input parameter types and explicit return type contracts.
    """
    def decorator(func: Callable[P, Awaitable[T]]) -> Callable[P, Awaitable[T]]:
        @functools.wraps(func)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
            logger = logging.getLogger(func.__module__)
            
            # Safely extract class logger (e.g. self.logger) if wrapping an object method
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