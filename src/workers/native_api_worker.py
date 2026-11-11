import asyncio
from PyQt5.QtCore import QObject, QRunnable, pyqtSignal
from api.native_velide import NativeApiError


class NativeSignals(QObject):
    result = pyqtSignal(object)
    failed = pyqtSignal(dict)


class NativeApiWorker(QRunnable):
    def __init__(self, gateway, operation, *args):
        super().__init__()
        self.signals = NativeSignals()
        self.gateway, self.operation, self.args = gateway, operation, args

    def run(self):
        try:
            self.signals.result.emit(asyncio.run(self._run()))
        except NativeApiError as error:
            self.signals.failed.emit({"message": str(error), "code": error.code, "retryable": error.retryable})
        except Exception as error:
            self.signals.failed.emit({"message": str(error), "code": "TRANSPORT_ERROR", "retryable": True})

    async def _run(self):
        client = self.gateway.get_client()
        if client is None:
            raise NativeApiError("Authenticate before synchronizing", "AUTH_REQUIRED", True)
        async with client:
            return await getattr(client, self.operation)(*self.args)
