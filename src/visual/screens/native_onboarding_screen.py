"""Interactive native setup. Invoke before building any runtime services."""
from html import escape
from threading import Event

from PyQt5.QtCore import QObject, QRunnable, QThreadPool, QTimer, Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QComboBox, QDialog, QFileDialog, QFormLayout, QHBoxLayout, QLabel,
    QLineEdit, QPushButton, QStackedWidget, QVBoxLayout, QWidget,
)

from native_onboarding import (
    NativeOnboarding, NativeOnboardingError,
    validate_source_setup,
)
from middleware_transport import MiddlewareError


class _TokenSink:
    """AuthService's token recipient; no API clients or service graph."""
    def update_token(self, token):
        self.token = token

    def invalidate_session(self):
        self.token = None


class _WorkerSignals(QObject):
    finished = pyqtSignal(bool, object)


class _Worker(QRunnable):
    def __init__(self, operation):
        super().__init__()
        self.operation = operation
        self.signals = _WorkerSignals()

    def run(self):
        try:
            result = self.operation()
        except Exception as error:
            self.signals.finished.emit(False, error)
        else:
            self.signals.finished.emit(True, result)


class NativeOnboardingScreen(QDialog):
    def __init__(self, config_path, bundle_dir, transport=None, parent=None,
                 auth_service_factory=None):
        super().__init__(parent)
        self.setWindowTitle("Configurar conexão com o Velide")
        self.setMinimumWidth(570)
        self.settings = None
        self.onboarding = None
        self.auth = None
        self._config_path = config_path
        self._bundle_dir = bundle_dir
        self._transport = transport
        self._auth_service_factory = auth_service_factory
        self._cancel_event = Event()
        self._busy = False
        self._closing = False
        self._token = None
        self._auth_paused = False
        self._merchants = []
        self._worker = None
        self._retry = self._bootstrap
        self._pool = QThreadPool.globalInstance()
        # Never wait on the global pool from one of its own workers. This pool
        # owns only the independent login/refresh/cache workers being handed off.
        self._auth_pool = QThreadPool(self)
        self._build_ui()
        QTimer.singleShot(0, self._bootstrap)

    def _build_ui(self):
        layout = QVBoxLayout(self)
        title = QLabel("Conecte sua loja ao Velide")
        title.setStyleSheet("font-size: 20px; font-weight: bold")
        layout.addWidget(title)
        self.status = QLabel("Carregando configuração de acesso…")
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.PlainText)
        layout.addWidget(self.status)
        self.login_button = QPushButton("Entrar no Velide")
        self.login_button.setEnabled(False)
        self.login_button.clicked.connect(self._login)
        layout.addWidget(self.login_button)
        self.device_code = QLabel()
        self.device_code.setTextFormat(Qt.PlainText)
        self.device_code.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.device_code)
        self.device_link = QLabel()
        self.device_link.setOpenExternalLinks(True)
        layout.addWidget(self.device_link)

        self.form = QWidget()
        form = QFormLayout(self.form)
        self.merchant = QComboBox()
        self.source = QComboBox()
        self.source.addItems(["Farmax", "CDS"])
        self.mode = QComboBox()
        self.mode.addItem("Escolha como conectar…", None)
        self.mode.addItem("Criar primeira conexão", "create")
        self.mode.addItem("Reconectar uma conexão existente", "reconnect")
        self.connection = QComboBox()
        form.addRow("Loja autorizada", self.merchant)
        form.addRow("Sistema de origem", self.source)
        form.addRow("Conexão", self.mode)
        form.addRow("Conexão existente", self.connection)
        self.timezone = QLabel()
        self.timezone.setTextFormat(Qt.PlainText)
        form.addRow("Fuso horário da loja", self.timezone)
        self.source_fields = QStackedWidget()
        farmax = QWidget()
        farmax_form = QFormLayout(farmax)
        self.farmax_host = QLineEdit("localhost")
        self.farmax_file = QLineEdit()
        self.farmax_user = QLineEdit("SYSDBA")
        self.farmax_password = QLineEdit()
        self.farmax_password.setEchoMode(QLineEdit.Password)
        for label, field in (("Servidor Firebird", self.farmax_host),
                             ("Arquivo do banco", self.farmax_file),
                             ("Usuário", self.farmax_user),
                             ("Senha", self.farmax_password)):
            farmax_form.addRow(label, field)
        self.source_fields.addWidget(farmax)
        cds = QWidget()
        cds_form = QFormLayout(cds)
        self.cds_folder = QLineEdit()
        folder_row = QHBoxLayout()
        folder_row.addWidget(self.cds_folder)
        browse = QPushButton("Selecionar pasta…")
        browse.clicked.connect(self._browse_folder)
        folder_row.addWidget(browse)
        cds_form.addRow("Pasta de pedidos", folder_row)
        self.source_fields.addWidget(cds)
        form.addRow(self.source_fields)
        note = QLabel(
            "Para reinstalação ou troca de computador, selecione Reconectar. "
            "Após concluir, o Velide recuperará os pedidos existentes e "
            "abrirá a configuração de entregadores.")
        note.setWordWrap(True)
        form.addRow(note)
        self.form.setEnabled(False)
        layout.addWidget(self.form)
        buttons = QHBoxLayout()
        self.retry_button = QPushButton("Tentar novamente")
        self.retry_button.hide()
        self.retry_button.clicked.connect(lambda: self._retry())
        self.cancel_button = QPushButton("Cancelar")
        self.cancel_button.clicked.connect(self.reject)
        self.finish_button = QPushButton("Concluir configuração")
        self.finish_button.setEnabled(False)
        self.finish_button.clicked.connect(self._finish)
        buttons.addWidget(self.retry_button)
        buttons.addStretch()
        buttons.addWidget(self.cancel_button)
        buttons.addWidget(self.finish_button)
        layout.addLayout(buttons)
        self.merchant.currentIndexChanged.connect(self._selection_changed)
        self.source.currentIndexChanged.connect(self._selection_changed)
        self.mode.currentIndexChanged.connect(self._selection_changed)

    def _browse_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Pasta de pedidos do CDS", self.cds_folder.text())
        if folder:
            self.cds_folder.setText(folder)

    def _selection_changed(self):
        previous = self.connection.currentData()
        self.connection.clear()
        self.connection.addItem("Selecione a conexão…", None)
        merchant = self.merchant.currentData()
        if merchant:
            self.timezone.setText(merchant["timezone"])
            for connection in merchant["connections"]:
                if connection["source"] == self.source.currentText() and connection["enabled"]:
                    identity = connection["connection_id"]
                    self.connection.addItem(identity, identity)
        else:
            self.timezone.clear()
        index = self.connection.findData(previous)
        if index >= 0:
            self.connection.setCurrentIndex(index)
        self.connection.setEnabled(self.mode.currentData() == "reconnect")
        self.source_fields.setCurrentIndex(0 if self.source.currentText() == "Farmax" else 1)

    def _set_busy(self, busy):
        self._busy = busy
        self.login_button.setEnabled(
            not busy and self.auth is not None and self._auth_pool.activeThreadCount() == 0)
        self.form.setEnabled(not busy and bool(self._merchants))
        self.finish_button.setEnabled(not busy and bool(self._merchants) and bool(self._token))
        self.retry_button.setEnabled(not busy)

    def _run(self, operation, completed, retry, message, closing=False):
        if self._busy or (self._closing and not closing):
            return
        self._retry = retry
        self._completed = completed
        self._set_busy(True)
        self.retry_button.hide()
        self.status.setText(message)
        self._worker = _Worker(operation)
        self._worker.signals.finished.connect(self._operation_finished)
        self._pool.start(self._worker)

    def _operation_finished(self, success, result):
        self._worker = None
        self._set_busy(False)
        # A committed result always wins a cancellation racing the final rename.
        if success:
            self._completed(result)
        elif not self._closing:
            self.status.setText(
                str(result) if isinstance(result, (NativeOnboardingError, MiddlewareError))
                else "Não foi possível concluir esta etapa. Verifique a conexão e tente novamente.")
            self.retry_button.show()
        if self._closing and self.settings is None:
            self._finish_closing()

    def _bootstrap(self):
        def prepare():
            if self._transport is None:
                from middleware_transport import MiddlewareTransport, service_url
                self._transport = MiddlewareTransport(service_url(self._bundle_dir))
            onboarding = self.onboarding or NativeOnboarding(
                self._config_path, self._bundle_dir, self._transport,
                cancel_event=self._cancel_event)
            return onboarding, onboarding.bootstrap()
        self._run(prepare, self._bootstrapped, self._bootstrap, "Carregando configuração de acesso…")

    def _bootstrapped(self, result):
        self.onboarding, auth_config = result
        if self._closing:
            return
        factory = self._auth_service_factory
        if factory is None:
            from services.auth_service import AuthService
            factory = AuthService
        self.auth = factory(auth_config, _TokenSink(), parent=self,
                            token_path=str(self.onboarding.token_path))
        self.auth._thread_pool = self._auth_pool
        self.auth.device_code.connect(self._show_device_code)
        self.auth.access_token.connect(self._authenticated)
        self.auth.error.connect(self._auth_error)
        self.login_button.setEnabled(True)
        self.status.setText("Entre no Velide para listar suas lojas autorizadas.")
        # The independent cache can resume a setup interrupted after login.
        self.auth.load_stored_token()

    def _login(self):
        if self.auth and not self._busy:
            if self._auth_paused:
                # A failed publication may be retried with a fresh grant, but
                # no second grant owner may start while the first is draining.
                if self._auth_pool.activeThreadCount():
                    return
                self.auth.resume_after_rollout()
                self._auth_paused = False
            self._token = None
            self._merchants = []
            self.merchant.clear()
            self.form.setEnabled(False)
            self.finish_button.setEnabled(False)
            self.device_code.clear()
            self.device_link.clear()
            self.retry_button.hide()
            self.status.setText("Preparando acesso seguro ao Velide…")
            self.auth.load_device_flow()

    def _show_device_code(self, code):
        if self._closing:
            return
        self.device_code.setText("Código de acesso: " + code["user_code"])
        url = code.get("verification_uri_complete") or code["verification_uri"]
        self.device_link.setText('<a href="{}">{}</a>'.format(
            escape(url, quote=True), escape(code["verification_uri"])))
        self.status.setText("Abra o link e confirme o código para entrar.")

    def _auth_error(self, title, details):
        if self._closing or self._busy:
            return
        self.status.setText("Entre no Velide para continuar ou renovar a sessão.")
        self.login_button.setEnabled(True)

    def _authenticated(self, token):
        if self._closing:
            return
        self._token = token
        if not self._busy and not self._auth_paused:
            self.device_code.clear()
            self.device_link.clear()
            self._load_options()

    def _load_options(self):
        token = self._token
        self._run(lambda: self.onboarding.options(token), self._options_loaded,
                  self._load_options, "Buscando lojas e conexões autorizadas…")

    def _options_loaded(self, response):
        if self._closing:
            return
        self._merchants = response["merchants"]
        self.merchant.clear()
        self.merchant.addItem("Selecione a loja…", None)
        for merchant in self._merchants:
            self.merchant.addItem(merchant["name"], merchant)
        request = self.onboarding.pending_request
        if request:
            for index in range(1, self.merchant.count()):
                if self.merchant.itemData(index)["merchant_id"] == request["merchant_id"]:
                    self.merchant.setCurrentIndex(index)
                    break
            self.source.setCurrentText(request["source"])
            self.mode.setCurrentIndex(self.mode.findData(request["mode"]))
            self.connection.setCurrentIndex(max(0, self.connection.findData(request.get("connection_id"))))
        self._set_busy(False)
        self.status.setText(
            "Selecione a loja, a conexão e os dados do seu sistema."
            if self._merchants else "Esta conta não possui lojas autorizadas para integração.")

    def _finish(self):
        merchant = self.merchant.currentData()
        mode = self.mode.currentData()
        connection_id = self.connection.currentData() if mode == "reconnect" else None
        if not merchant or not mode or (mode == "reconnect" and not connection_id):
            self.status.setText("Selecione a loja e escolha criar ou reconectar uma conexão.")
            return
        source = self.source.currentText()
        farmax = dict(host=self.farmax_host.text(), file=self.farmax_file.text(),
                      user=self.farmax_user.text(), password=self.farmax_password.text())
        folder = self.cds_folder.text()
        def finish():
            validate_source_setup(source, merchant["timezone"], farmax, folder)
            token = self.onboarding.prepared_session()["access_token"]
            self.onboarding.connect(token, merchant["merchant_id"], source, mode, connection_id)
            return self.onboarding.complete(token, farmax=farmax, folder_to_watch=folder)
        self._publish_operation = finish
        self._pause_auth()
        self._run(self._wait_auth, self._auth_drained, self._finish,
                  "Concluindo a sessão de acesso…")

    def _pause_auth(self):
        if self.auth and not self._auth_paused:
            self._auth_paused = True
            # In-flight refreshes must persist their rotated grant before
            # invalidation; pause suppresses new refreshes and late publications.
            self.auth.pause_for_rollout()

    def _wait_auth(self):
        return self._auth_pool.waitForDone(20000)

    def _auth_drained(self, drained):
        if self._closing:
            return
        if not drained:
            self.status.setText("A autenticação ainda está concluindo uma solicitação. Tente novamente.")
            self.retry_button.show()
            return
        self.auth.invalidate_session()
        self._run(self._publish_operation, self._published, self._finish,
                  "Verificando acesso e salvando configuração…")

    def _published(self, settings):
        self.settings = settings
        self._close_auth()
        super().accept()

    def _close_auth(self):
        if self.auth:
            self.auth.invalidate_session()

    def _finish_closing(self):
        if self._auth_pool.activeThreadCount():
            self._run(self._wait_auth, lambda _: None, self.reject,
                      "Concluindo o cancelamento da autenticação…", closing=True)
        else:
            self._close_auth()
            super().reject()

    def reject(self):
        self._closing = True
        self._cancel_event.set()
        self._pause_auth()
        if self._busy:
            self.status.setText("Cancelando configuração…")
            self.cancel_button.setEnabled(False)
            return
        self._finish_closing()

    def closeEvent(self, event):
        # reject() closes only after both pools are safe. An accepted native
        # window close here would otherwise end exec_ while auth is draining.
        event.ignore()
        self.reject()


NativeOnboardingDialog = NativeOnboardingScreen


def run_native_onboarding(config_path, bundle_dir, transport=None, parent=None):
    """QApplication and instance lock must exist; return published Settings/None."""
    dialog = NativeOnboardingScreen(config_path, bundle_dir, transport, parent)
    if dialog.exec_() == QDialog.Accepted:
        return dialog.settings
    return None
