import asyncio
from PyQt5.QtCore import QObject, QRunnable, pyqtSignal
from api.native_velide import NativeApiError
from api.velide_gateway import freeze_gateway


class NativeSignals(QObject):
    result = pyqtSignal(object)
    failed = pyqtSignal(dict)


class NativeApiWorker(QRunnable):
    def __init__(self, gateway, operation, *args, preflight=None):
        super().__init__()
        self.signals = NativeSignals()
        self.gateway, self.operation, self.args = freeze_gateway(gateway), operation, args
        self.preflight = preflight

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
            if self.preflight is not None and not self.preflight():
                raise NativeApiError("Local source/operation precondition changed before sending", "LOCAL_PRECONDITION", False)
            return await getattr(client, self.operation)(*self.args)
