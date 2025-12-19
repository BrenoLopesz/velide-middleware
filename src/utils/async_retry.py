import asyncio
import functools
import logging

import httpx

from utils.exception_translator import get_friendly_error_msg

def async_retry(
    operation_desc: str,
    max_retries: int = 3, 
    initial_delay: float = 1.0, 
    backoff_factor: float = 2.0, 
    exceptions: tuple = (httpx.RequestError, httpx.TimeoutException)
):
    """
    Decorator with user-friendly logging.
    """
    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            delay = initial_delay
            last_exception = None
            
            # Setup Logger
            logger = logging.getLogger(func.__module__)
            if args and hasattr(args[0], 'logger'):
                logger = args[0].logger

            for attempt in range(1, max_retries + 1):
                try:
                    return await func(*args, **kwargs)
                except exceptions as e:
                    last_exception = e
                    
                    # 1. Translate the error
                    friendly_error = get_friendly_error_msg(e)
                    
                    # 2. Check if we should stop
                    if attempt == max_retries:
                        break 
                    
                    # 3. Log the friendly message
                    # Example: "Tentativa 1/3 de Enviar Entrega falhou: Falha na conexão. Aguardando 1.0s..."
                    logger.warning(
                        f"Tentativa {attempt}/{max_retries} de {operation_desc} falhou: "
                        f"{friendly_error}. Aguardando {delay}s..."
                    )
                    
                    await asyncio.sleep(delay)
                    delay *= backoff_factor
            
            # Final failure log before raising
            logger.error(f"Todas as tentativas de {operation_desc} falharam.")
            raise last_exception
        return wrapper
    return decorator