import asyncio
import logging

from PyQt5.QtCore import QObject, pyqtSlot, Qt, QTimer

from services.auth_service import AuthService

class AsyncTokenProvider:
    """
    A bridge utility to await Qt Signal-based authentication from an asyncio loop.

    This class solves a specific architectural challenge: bridging the Main Thread 
    (where Qt UI/Signals live) and a Worker Thread (running an asyncio loop without 
    a Qt Event Loop).

    It uses `Qt.DirectConnection` to bypass the Worker Thread's missing event loop, 
    forcing signal callbacks to execute immediately on the Main Thread, which then 
    safely passes data to the asyncio loop via `call_soon_threadsafe`.
    """
    
    _logger = logging.getLogger(__name__)

    class _SignalReceiver(QObject):
        """
        Internal lightweight receiver to bridge Qt Signals to asyncio Futures.
        
        This object acts as an anchor for Qt signals. Its methods are designed 
        to be called directly from the Main Thread (via DirectConnection) to 
        safely resolve a future belonging to the Worker Thread.
        """
        def __init__(self, loop: asyncio.AbstractEventLoop, future: asyncio.Future):
            super().__init__()
            self._loop = loop
            self._future = future

        @pyqtSlot(str)
        def on_token(self, token: str) -> None:
            """
            Callback for successful token retrieval.
            
            Executed on: Main Thread (via DirectConnection).
            Action: Thread-safe transfer to asyncio loop.
            """
            if not self._future.done():
                self._loop.call_soon_threadsafe(self._future.set_result, token)

        @pyqtSlot(str, str)
        def on_error(self, title: str, msg: str) -> None:
            """
            Callback for authentication errors.
            
            Executed on: Main Thread (via DirectConnection).
            Action: Thread-safe exception setting on asyncio loop.
            """
            if not self._future.done():
                error_msg = f"Auth Failure [{title}]: {msg}"
                self._loop.call_soon_threadsafe(
                    self._future.set_exception, RuntimeError(error_msg)
                )

    @classmethod
    async def get_valid_token(
        cls, 
        auth_service: AuthService, 
        timeout: float = 10.0
    ) -> str:
        """
        Triggers token retrieval on the Main Thread and 
        awaits the result asynchronously.

        Args:
            auth_service (QObject): The service exposing `load_stored_token` (slot) 
                                    and `access_token` / `error` (signals).
            timeout (float): Maximum time to wait for the token in seconds.

        Returns:
            str: The valid access token.

        Raises:
            asyncio.TimeoutError: If the service does not respond within the timeout.
            RuntimeError: If the service emits an error signal or fails invocation.
        """
        loop = asyncio.get_running_loop()
        future = loop.create_future()

        # 1. Create Receiver
        # This object technically lives in the Worker Thread (Python-side),
        # but its slots will be invoked on the Main Thread due to DirectConnection.
        receiver = cls._SignalReceiver(loop, future)

        # 2. Connect Signals (The "Direct" Bridge)
        # standard connect() would create a QueuedConnection because threads differ.
        # QueuedConnection requires the Worker Thread to have a running QEventLoop 
        # (exec_()), which it does not (it has an asyncio loop).
        # We force DirectConnection so the callback runs on the EMITTER'S thread (Main).
        auth_service.access_token.connect(
            receiver.on_token, Qt.DirectConnection
        ) # type: ignore[call-arg]
        auth_service.error.connect(
            receiver.on_error, Qt.DirectConnection
        ) # type: ignore[call-arg]

        try:
            # 3. Trigger Action (Thread-Safe)
            # QTimer.singleShot(0, ...) is the robust standard for "Run this on the
            # target object's thread ASAP". It handles the thread jump automatically.
            QTimer.singleShot(0, auth_service.load_stored_token)

            # 4. Await Result
            return await asyncio.wait_for(future, timeout=timeout)

        except asyncio.TimeoutError:
            cls._logger.error(
                f"Tempo excedido ({timeout}s) ao aguardar por token de autenticação."
            )
            raise

        except Exception:
            # Catching generic errors to ensure logging context before re-raising
            cls._logger.exception("Falha inesperada ao buscar token de acesso.")
            raise

        finally:
            # 5. Cleanup
            # We must disconnect to prevent memory leaks or callbacks firing 
            # after the future is abandoned.
            try:
                auth_service.access_token.disconnect(receiver.on_token)
            except (TypeError, RuntimeError):
                pass
            
            try:
                auth_service.error.disconnect(receiver.on_error)
            except (TypeError, RuntimeError):
                pass
            
            # Request C++ cleanup. 
            # Note: Without a QEventLoop in this thread, this might be delayed 
            # until thread exit, but explicit disconnects above handle the logic safety.
            receiver.deleteLater()