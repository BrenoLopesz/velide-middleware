"""Qt startup/discovery/device-login bridge for the headless rollout coordinator."""
import queue
import threading
import time

from PyQt5.QtCore import QObject, QEventLoop, QThreadPool, QTimer, pyqtSignal
from PyQt5.QtWidgets import QAction, QDialog, QLabel, QPushButton, QVBoxLayout

from config import AuthenticationConfig
from middleware_rollout import RolloutCoordinator, committed_receipt
from middleware_transport import MiddlewareTransport, MiddlewareError, service_url
from services.auth_service import AuthService
from utils.token_storage import read_token_from_file


class QtMiddlewareTransport(MiddlewareTransport):
    """Keep Qt responsive while the bounded transport works off the GUI thread."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.cancel_requested = False

    def request(self, *args, **kwargs):
        completed = queue.Queue(maxsize=1)
        def run():
            try:
                completed.put((super(QtMiddlewareTransport, self).request(*args, **kwargs), None))
            except BaseException as error:
                completed.put((None, error))
        threading.Thread(target=run, name="middleware-qt-http", daemon=True).start()
        loop, timer = QEventLoop(), QTimer()
        timer.timeout.connect(lambda: loop.quit() if not completed.empty() or self.cancel_requested else None)
        timer.start(25)
        loop.exec_()
        timer.stop()
        if self.cancel_requested:
            raise MiddlewareError("CANCELLED", "Atualização cancelada; a conexão legada continuará ativa", True)
        result, error = completed.get_nowait()
        if error is not None:
            raise error
        return result


class _AuthGateway:
    def update_token(self, token):
        self.token = token

    def invalidate_session(self):
        self.token = None


class RolloutController(QObject):
    activated = pyqtSignal(object)

    def __init__(self, runtime, config_path, bundle_dir, transport=None):
        super().__init__(runtime)
        self.runtime = runtime
        self.busy = False
        self._retired = False
        self._closed = False
        self.native_auth = None
        self._auth_pool = QThreadPool(self)
        self._retired_auth = []
        self.dialog = QDialog()
        self.dialog.setWindowTitle("Atualização para o novo Velide")
        layout = QVBoxLayout(self.dialog)
        self.message = QLabel("Verificando disponibilidade da nova conexão…")
        self.message.setWordWrap(True)
        self.message.setOpenExternalLinks(True)
        layout.addWidget(self.message)
        self.login = QPushButton("Entrar no novo Velide")
        self.finish = QPushButton("Concluir atualização")
        self.retry = QPushButton("Verificar novamente")
        self.cancel_button = QPushButton("Cancelar atualização")
        for button in (self.login, self.finish, self.retry, self.cancel_button):
            layout.addWidget(button)
        self.login.clicked.connect(self.start_login)
        self.finish.clicked.connect(self.begin)
        self.retry.clicked.connect(self.discover)
        self.cancel_button.clicked.connect(self.cancel)
        self.dialog.rejected.connect(self.cancel)
        self.login.setEnabled(False)
        self.finish.setEnabled(False)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.tick)
        action = QAction("Configuração / atualização do novo Velide", runtime.tray.contextMenu())
        action.triggered.connect(self.dialog.show)
        runtime.tray.contextMenu().addAction(action)
        self.action = action
        self.coordinator = None
        try:
            from services.rollout_quiescence import GraphQuiescence
            self.transport = transport or QtMiddlewareTransport(service_url(bundle_dir))
            legacy = runtime.runtimes.get("legacy")
            provider = lambda: legacy.services.auth._bundle_access_token if legacy else None
            self.coordinator = RolloutCoordinator(config_path, bundle_dir, self.transport, provider,
                                                 GraphQuiescence(runtime.runtimes), notify=self.show_state)
            if hasattr(getattr(self.coordinator.graph, "resumed", None), "connect"):
                self.coordinator.graph.resumed.connect(self._resumed)
        except (ValueError, OSError) as error:
            self.message.setText(str(error))
            self.retry.setEnabled(False)
            return
        for name, profile in runtime.runtimes.items():
            profile.services.auth.access_token.connect(lambda token, name=name: self.on_token(name, token))
        self.timer.start(1000)

    def on_token(self, name, token):
        if self._retired or self.busy or not self.coordinator:
            return
        if self.coordinator.state["state"] == "ACK_PENDING" and name == "native":
            self._perform(lambda: self.coordinator.acknowledge(token))
            self.schedule()
        elif name == "legacy":
            self.timer.start(100)

    def _handoff_if_committed(self, result=None):
        # Durable ACK_PENDING/APPLIED also describe an already-active native
        # runtime. Only this controller's paused legacy graph needs rebuilding.
        if self._retired or not self.coordinator._paused:
            return False
        if self.coordinator.state["state"] not in {"ACK_PENDING", "APPLIED"} and not committed_receipt(
                self.coordinator.config_path, self.coordinator.bundle_dir, self.coordinator.state):
            return False
        # Retire BEFORE emitting: the synchronous rebuild can shut us down, and
        # queued token/timer callbacks must not rebuild again or rearm this timer.
        self._retired = True
        self.timer.stop()
        self.dialog.hide()
        self.activated.emit(result)
        return True

    def _perform(self, operation):
        if self._retired or self.busy or self.coordinator is None:
            return None
        self.busy = True
        self.transport.cancel_requested = False
        self.retry.setEnabled(False)
        self.login.setEnabled(False)
        self.finish.setEnabled(False)
        try:
            result = operation()
            self._handoff_if_committed(result)
            return result
        except Exception as error:
            # Invalidation disk failure deliberately leaves the graph blocked;
            # never claim resumed service or enable another final inventory.
            self.message.setText("Não foi possível concluir a atualização: " + str(error))
            if not self._handoff_if_committed() and self.coordinator.state["state"] in {
                    "PAUSING", "WAITING", "READY", "APPLYING"}:
                self.message.setText("Recuperação necessária antes de continuar: " + str(error))
            return None
        finally:
            self.busy = False
            self.transport.cancel_requested = False
            if not self._retired:
                state = self.coordinator.state["state"]
                self.retry.setEnabled(state not in {"PAUSING", "WAITING", "READY", "APPLYING"} and
                                      getattr(self.coordinator.graph, "state", "running") == "running")
                self.login.setEnabled(state in {"AWAITING_LOGIN", "PREPARED"})
                self.finish.setEnabled(state == "PREPARED")

    def discover(self):
        if self._retired:
            return
        self._perform(self.coordinator.discover if self.coordinator else lambda: None)
        if self.coordinator and self.coordinator.state["state"] in {"AWAITING_LOGIN", "PREPARED"}:
            self.dialog.show()
        self.schedule()

    def schedule(self):
        if self._retired or not self.coordinator:
            return
        state = self.coordinator.state
        if state["state"] == "ACK_PENDING":
            self.timer.start(30000)
        elif state["state"] not in {"APPLIED", "PREPARED", "AWAITING_LOGIN"}:
            self.timer.start(int(max(1, min(300, state.get("retry_after_seconds", 30))) * 1000))
        else:
            self.timer.stop()

    def tick(self):
        if self._retired or self.busy or self.coordinator is None:
            return
        state = self.coordinator.state["state"]
        if state == "INVALIDATED" and getattr(self.coordinator.graph, "state", "running") != "running":
            self.timer.start(100)
            return
        if state in {"WAITING", "READY"}:
            self._perform(self.coordinator.poll)
            self.schedule()
        elif state == "ACK_PENDING":
            native = self.runtime.runtimes.get("native")
            token = native.services.auth._bundle_access_token if native else None
            if token:
                self._perform(lambda: self.coordinator.acknowledge(token))
            self.schedule()
        elif state not in {"APPLIED", "PREPARED", "AWAITING_LOGIN"}:
            self.discover()

    def _resumed(self):
        if self._retired:
            return
        self.message.setText("A sincronização legada foi retomada. Verifique novamente para iniciar outra tentativa.")
        self.retry.setEnabled(True)

    def show_state(self, state):
        labels = {
            "DISABLED": "A conexão legada está ativa. A atualização ainda não foi habilitada.",
            "AWAITING_LOGIN": "Entre no novo Velide para preparar a atualização. A conexão legada continua funcionando.",
            "PREPARED": "Nova conexão autorizada. Clique em concluir para sincronizar o inventário final.",
            "PAUSING": "Preparando a atualização e aguardando as operações em andamento…",
            "WAITING": "Inventário final enviado. Aguardando preparação no servidor (prazo limitado)…",
            "READY": "Pacote validado pelo servidor. Preparando aplicação…",
            "APPLYING": "Aplicando a nova conexão…",
            "ACK_PENDING": "Nova conexão ativa. A confirmação será reenviada automaticamente.",
            "APPLIED": "Nova conexão ativa.",
            "INVALIDATED": "Tentativa encerrada. A conexão legada pode continuar; verifique novamente para uma nova tentativa.",
        }
        self.message.setText(labels.get(state["state"], state["state"]))

    def start_login(self):
        if self._retired or self.busy or not self.coordinator:
            return
        self.timer.stop()
        if self.native_auth:
            self.native_auth.invalidate_session()
            self._retired_auth.append(self.native_auth)
        auth = AuthenticationConfig.model_validate(self.coordinator.state["target"]["auth"])
        self.native_auth = AuthService(auth, _AuthGateway(), parent=self,
                                       token_path=str(self.coordinator.prepared_path))
        self.native_auth._thread_pool = self._auth_pool
        self.native_auth.device_code.connect(self.display_code)
        self.native_auth.error.connect(lambda title, message: self.message.setText(title + ": " + message))
        self.native_auth.access_token.connect(self.prepared)
        self.native_auth.load_device_flow()
        self.login.setEnabled(False)

    def display_code(self, code):
        import html
        url = html.escape(code.get("verification_uri_complete") or code.get("verification_uri", ""), quote=True)
        self.message.setText('Entre no novo Velide: <a href="{}">{}</a><br>Código: {}'.format(
            url, url, html.escape(code.get("user_code", ""))))

    def prepared(self, token):
        if self._retired or self.busy or not self.native_auth:
            return
        self._perform(lambda: self.coordinator.prepare(read_token_from_file(self.coordinator.prepared_path)))

    def _freeze_native_auth(self):
        if not self.native_auth:
            raise MiddlewareError("LOGIN_REQUIRED", "Entre no novo Velide novamente", True)
        # Let a rotating grant commit before fencing it; dropping a successful
        # refresh response could invalidate the only persisted refresh token.
        self.native_auth.pause_for_rollout()
        end = time.monotonic() + 20
        while not self._auth_pool.waitForDone(0):
            if time.monotonic() >= end:
                raise MiddlewareError("LOGIN_BUSY", "Aguarde a atualização da sessão e tente novamente", True)
            loop, timer = QEventLoop(), QTimer()
            timer.setSingleShot(True)
            timer.timeout.connect(loop.quit)
            timer.start(20)
            loop.exec_()
        self.native_auth._refresh_timer.stop()
        self.native_auth.invalidate_session()
        self.coordinator.prepare(read_token_from_file(self.coordinator.prepared_path))

    def begin(self):
        def run():
            self._freeze_native_auth()
            return self.coordinator.begin()
        self._perform(run)
        self.schedule()

    def cancel(self):
        if self._retired or not self.coordinator:
            return
        if self.busy:
            self.transport.cancel_requested = True
            try:
                self.coordinator.request_cancel()
                self.message.setText("Tentativa cancelada. Aguardando as operações já iniciadas para retomar o legado…")
            except (ValueError, OSError) as error:
                self.message.setText("Recuperação necessária: " + str(error))
            return
        if self.native_auth:
            self.native_auth.invalidate_session()
        if self.coordinator.state["state"] not in {"ACK_PENDING", "APPLIED"}:
            self._perform(lambda: self.coordinator.cancel())
        self.schedule()

    def shutdown(self):
        if self._closed:
            return
        self._closed = self._retired = True
        self.timer.stop()
        if self.native_auth:
            self.native_auth.invalidate_session()
        if self.coordinator and self.coordinator.state["state"] in {"PAUSING", "WAITING", "READY", "APPLYING"}:
            if committed_receipt(self.coordinator.config_path, self.coordinator.bundle_dir, self.coordinator.state):
                self.coordinator._release()
            else:
                self.coordinator.cancel("restart")
        self.dialog.rejected.disconnect(self.cancel)
        self.dialog.close()
