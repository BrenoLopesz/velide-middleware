"""Native-only setup, before constructing the runtime service graph.

The caller holds the application instance lock. Network methods are synchronous
so a CLI can use them directly; the Qt adapter runs them in workers. Transport
responses follow MIDDLEWARE_ROLLOUT_PROTOCOL.md. No legacy gateway is constructed.

Flow: bootstrap(), options(token), connect(token, merchant, source, mode, ...),
complete(token, farmax=... / folder_to_watch=...). AuthService can write directly
to token_path; headless callers can instead pass token_bundle to complete().
The config replacement is the sole activation commit point.
"""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from threading import Event
import uuid

from filelock import FileLock
import yaml


class NativeOnboardingError(ValueError):
    """An actionable setup failure, safe to display without credential details."""


class NativeOnboardingCancelled(NativeOnboardingError):
    pass


def _atomic_write(path, contents, before_replace=None):
    descriptor, temporary = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(contents)
            stream.flush()
            os.fsync(stream.fileno())
        if before_replace:
            before_replace()
        os.replace(temporary, str(path))
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _absolute(path, root):
    path = Path(path)
    return (path if path.is_absolute() else root / path).resolve()


def _has_database_data(path):
    # Empty, packaged schemas are fine. Even one mapping/ownership/scope row
    # means this is an installed system, not a new installation.
    if not path.exists() or path.stat().st_size == 0:
        return False
    if any(Path(str(path) + suffix).exists()
           and Path(str(path) + suffix).stat().st_size
           for suffix in ("-wal", "-journal")):
        return True
    try:
        # Nonempty sidecars are rejected above. Immutable reads avoid creating
        # SQLite WAL/SHM files merely by inspecting a packaged empty schema.
        connection = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)
        try:
            tables = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            return any(
                connection.execute('SELECT 1 FROM "' + name.replace('"', '""') +
                                   '" LIMIT 1').fetchone()
                for (name,) in tables if not name.startswith("sqlite_"))
        finally:
            connection.close()
    except sqlite3.Error as error:
        raise NativeOnboardingError(
            "Há um banco local que não pôde ser verificado. Restaure a "
            "configuração da instalação antes de continuar.") from error


def validate_source_setup(source, timezone, farmax=None, folder_to_watch=None):
    """Validate local fields only; never connect to or modify the ERP."""
    if source == "Farmax":
        from config import FarmaxConfig
        if not isinstance(farmax, dict) or any(
                not isinstance(farmax.get(key), str) or not farmax[key].strip()
                for key in ("host", "file", "user", "password")):
            raise NativeOnboardingError(
                "Informe servidor, arquivo, usuário e senha do Firebird.")
        values = deepcopy(farmax)
        if values.get("timezone", timezone) != timezone:
            raise NativeOnboardingError(
                "O fuso horário do Farmax deve ser o mesmo da loja.")
        values["timezone"] = timezone
        return {"farmax": FarmaxConfig.model_validate(values).model_dump(),
                "folder_to_watch": None}
    if source == "CDS":
        if not isinstance(folder_to_watch, str) or not folder_to_watch.strip():
            raise NativeOnboardingError("Informe a pasta de pedidos do CDS.")
        folder = Path(folder_to_watch).expanduser().resolve()
        if not folder.is_dir():
            raise NativeOnboardingError("A pasta de pedidos do CDS não existe.")
        return {"farmax": None, "folder_to_watch": str(folder)}
    raise NativeOnboardingError("Selecione Farmax ou CDS.")


class NativeOnboarding:
    def __init__(self, config_path, bundle_dir, transport, cancel_event=None):
        self.config_path = Path(config_path).absolute()
        self.bundle_dir = Path(bundle_dir).resolve()
        self.transport = transport
        self.state_path = self.config_path.parent / "native-onboarding.json"
        self.token_path = self.bundle_dir / "resources" / "native-onboarding-token.txt"
        self._cancelled = cancel_event if cancel_event is not None else Event()
        self._auth = None
        self._options = None
        self._options_principal = None
        self._lock = FileLock(str(self.config_path) + ".upgrade.lock", timeout=0)
        if not self.config_path.is_file():
            raise NativeOnboardingError(
                "Configuração ausente. Restaure a configuração existente ou "
                "instale o modelo de configuração nativa.")
        if any(path.is_symlink() for path in
               (self.config_path, self.state_path, self.token_path)):
            raise NativeOnboardingError("Os arquivos de instalação devem ser locais e próprios.")
        with self._lock:
            self._raw = self.config_path.read_bytes()
            self._document = yaml.safe_load(self._raw.decode("utf-8-sig"))
            if not isinstance(self._document, dict) or self._document.get("onboarding") != "native_required":
                raise NativeOnboardingError("Esta instalação não está aguardando configuração nativa.")
            if any(self._document.get(key) for key in (
                    "auth", "api", "backend_profiles", "active_backend",
                    "backend_ownership_path", "backend_ownership_id")):
                raise NativeOnboardingError(
                    "A configuração contém uma conexão instalada. Restaure a configuração original.")
            self.database_path = _absolute(
                self._document.get("sqlite_path", "resources/velide.db"), self.bundle_dir)
            self._state = self._read_state()
            self._reject_retained_data()
            if self._state is None:
                self._state = {
                    "version": 1, "installation_id": str(uuid.uuid4()),
                    "config_path": str(self.config_path),
                    "config_sha256": hashlib.sha256(self._raw).hexdigest(),
                    "token_path": str(self.token_path),
                }
                self._save_state(self._state)

    @property
    def installation_id(self):
        return self._state["installation_id"]

    @property
    def pending_request(self):
        return deepcopy(self._state.get("request"))

    def cancel(self):
        self._cancelled.set()

    def _check_cancelled(self):
        if self._cancelled.is_set():
            raise NativeOnboardingCancelled("Configuração cancelada.")

    def _read_state(self):
        if not self.state_path.exists():
            return None
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
            if (state["version"] != 1
                    or str(uuid.UUID(state["installation_id"])) != state["installation_id"]
                    or state["config_path"] != str(self.config_path)
                    or state["config_sha256"] != hashlib.sha256(self._raw).hexdigest()
                    or state["token_path"] != str(self.token_path)):
                raise ValueError()
            return state
        except (ValueError, KeyError, TypeError) as error:
            raise NativeOnboardingError(
                "O estado de configuração não corresponde a esta instalação. "
                "Restaure os arquivos originais.") from error

    def _save_state(self, state):
        self._check_cancelled()
        _atomic_write(self.state_path, json.dumps(
            state, ensure_ascii=True, sort_keys=True).encode("utf-8"))
        self._state = deepcopy(state)

    def _assert_current(self):
        self._check_cancelled()
        if (not self.config_path.is_file()
                or self.config_path.read_bytes() != self._raw
                or self._read_state() != self._state):
            raise NativeOnboardingError(
                "A configuração foi alterada durante a instalação. Reinicie o aplicativo.")
        self._reject_retained_data()

    def _reject_retained_data(self):
        databases = {self.database_path, self.bundle_dir / "resources" / "velide.db"}
        credentials = {
            self.bundle_dir / "resources" / "token.txt",
            _absolute(self._document.get("token_path") or "resources/token.txt", self.bundle_dir),
        }
        directories = {self.config_path.parent, self.bundle_dir / "resources",
                       self.database_path.parent}
        for directory in directories:
            if not directory.is_dir():
                continue
            for path in directory.iterdir():
                name = path.name.lower()
                if path == self.token_path:
                    if not self._state and path.exists():
                        raise NativeOnboardingError(
                            "Há uma sessão nativa sem o estado da instalação. Restaure o estado original.")
                    continue
                if not path.is_file():
                    continue
                if name.endswith((".db", ".sqlite", ".sqlite3")):
                    databases.add(path.resolve())
                elif (".db-" in name or ".db." in name
                      or ".sqlite-" in name or ".sqlite3-" in name
                      or name == "native-upgrade.json"
                      or "rollout" in name or ".pre-native-" in name):
                    # Journals, migration packages and backup copies are evidence
                    # of retained work even when the main database is missing.
                    if path.stat().st_size:
                        raise NativeOnboardingError(
                            "Há dados de uma instalação anterior. Restaure sua configuração antes de continuar.")
                if "token" in name and name.endswith(".txt"):
                    credentials.add(path.resolve())
        for path in credentials:
            if path != self.token_path and path.exists() and path.stat().st_size:
                raise NativeOnboardingError(
                    "Há credenciais de uma instalação anterior. Restaure sua configuração.")
        if any(_has_database_data(path) for path in databases):
            raise NativeOnboardingError(
                "Há dados de uma instalação anterior. Restaure sua configuração antes de continuar.")
        if self.database_path in (self.config_path, self.state_path, self.token_path):
            raise NativeOnboardingError("Os arquivos de configuração, sessão e banco devem ser separados.")

    def bootstrap(self):
        """Return AuthenticationConfig for an independent native AuthService."""
        from config import AuthenticationConfig
        from workers.stored_token_retriever_worker import auth_context
        with self._lock:
            self._assert_current()
            response = self.transport.bootstrap()
            auth = AuthenticationConfig.model_validate(response["auth"])
            context = auth_context(auth)
            previous = self._state.get("auth")
            if previous is not None and previous != context:
                raise NativeOnboardingError(
                    "A autenticação do servidor mudou durante a configuração.")
            self._assert_current()
            self._save_state(dict(self._state, auth=context))
            self._auth = auth
            return auth

    def _principal(self, token):
        if self._auth is None:
            raise NativeOnboardingError("Carregue a configuração de autenticação primeiro.")
        from backend_profiles import token_principal
        try:
            principal = token_principal(token)
        except ValueError as error:
            raise NativeOnboardingError("Entre novamente para continuar.") from error
        if (self._state.get("request")
                and self._state.get("principal") not in (None, principal)):
            raise NativeOnboardingError(
                "Esta configuração foi iniciada por outra conta. Entre com a conta original.")
        return principal

    def options(self, native_token):
        with self._lock:
            self._assert_current()
            principal = self._principal(native_token)
            response = self.transport.options(native_token)
            self._assert_current()
            # This continuity hint is recorded only after the server accepted
            # the native token. It is never used instead of server authorization.
            self._save_state(dict(self._state, principal=principal))
            self._options = deepcopy(response)
            self._options_principal = principal
            return deepcopy(response)

    def _validate_target(self, target, request):
        from config import ApiConfig
        from middleware_transport import validate_target
        validate_target(target, allow_loopback=getattr(self.transport, "allow_loopback", False))
        if (not isinstance(target, dict) or set(target) != {"auth", "api"}
                or target["auth"] != self._state["auth"]):
            raise NativeOnboardingError(
                "O destino retornado não corresponde à autenticação preparada.")
        api = target["api"]
        if (not isinstance(api, dict)
                or set(api) != {"velide_server", "velide_websockets_server", "use_ssl", "native"}
                or not isinstance(api.get("native"), dict)
                or set(api["native"]) != {"merchant_id", "connection_id", "timezone"}):
            raise NativeOnboardingError("O servidor retornou um destino nativo inválido.")
        parsed = ApiConfig.model_validate(api)
        if (parsed.native.merchant_id != request["merchant_id"]
                or (request["mode"] == "reconnect"
                    and parsed.native.connection_id != request["connection_id"])):
            raise NativeOnboardingError("O servidor retornou outra loja ou conexão.")
        return deepcopy(target)

    def connect(self, native_token, merchant_id, source, mode, connection_id=None):
        """Provision idempotently, or reconnect only an explicitly chosen owner."""
        with self._lock:
            self._assert_current()
            principal = self._principal(native_token)
            if self._options is None or principal != self._options_principal:
                raise NativeOnboardingError("Atualize a lista de lojas autorizadas.")
            merchant = next((item for item in self._options["merchants"]
                             if item["merchant_id"] == merchant_id), None)
            if merchant is None or source not in ("Farmax", "CDS"):
                raise NativeOnboardingError("Selecione uma loja autorizada e a origem dos pedidos.")
            if mode not in ("create", "reconnect") or (mode == "create" and connection_id is not None):
                raise NativeOnboardingError("Escolha criar ou reconectar explicitamente.")
            request = dict(merchant_id=merchant_id, source=source, mode=mode)
            if mode == "reconnect":
                if not any(item["connection_id"] == connection_id
                           and item["source"] == source and item["enabled"] is True
                           for item in merchant["connections"]):
                    raise NativeOnboardingError("Selecione uma conexão ativa e autorizada para reconectar.")
                request["connection_id"] = connection_id
            previous = self._state.get("request")
            if previous:
                if (previous["merchant_id"], previous["source"]) != (merchant_id, source):
                    raise NativeOnboardingError("Continue a configuração da loja e origem já selecionadas.")
                if self._state.get("target") and previous != request:
                    raise NativeOnboardingError("Continue a configuração da conexão já preparada.")
            if (mode == "create" and previous != request
                    and any(item["source"] == source for item in merchant["connections"])):
                raise NativeOnboardingError(
                    "Esta loja já possui uma conexão para esta origem. Escolha Reconectar.")
            # Save intent BEFORE the request: a lost response must reuse the UUID
            # and the same merchant/source, never create another namespace.
            self._save_state(dict(self._state, request=request))
            response = self.transport.connect(
                native_token, self.installation_id, merchant_id, source, mode,
                connection_id=connection_id)
            self._assert_current()
            if response["installation_id"] != self.installation_id or response["mode"] != mode:
                raise NativeOnboardingError("A resposta não pertence a esta instalação.")
            target = self._validate_target(response["target"], request)
            if self._state.get("target") not in (None, target):
                raise NativeOnboardingError("A conexão preparada foi alterada no servidor.")
            self._save_state(dict(self._state, target=target))
            return deepcopy(target)

    def prepared_session(self):
        """Read only after the independent AuthService has paused and drained."""
        from utils.token_storage import read_token_from_file
        return self._validated_bundle(read_token_from_file(self.token_path))

    def _validated_bundle(self, bundle, native_token=None):
        from middleware_transport import has_explicit_grant_scope
        from workers.stored_token_retriever_worker import auth_context, stored_session_matches
        if (self._auth is None or not isinstance(bundle, dict) or not has_explicit_grant_scope(bundle)
                or bundle.get("middleware_auth") != auth_context(self._auth)
                or not isinstance(bundle.get("access_token"), str)
                or not bundle["access_token"]
                or (native_token is not None and bundle["access_token"] != native_token)
                or not isinstance(bundle.get("refresh_token"), str)
                or not bundle["refresh_token"]):
            raise NativeOnboardingError(
                "A sessão nativa não foi salva para esta autenticação. Entre novamente.")
        # Cache metadata cannot relabel a grant from another issuer/client/API.
        # Check the actual claims using the existing cache-eligibility rules;
        # server verify still owns signature, expiry and permission checks.
        probe = dict(bundle)
        probe.pop("middleware_auth", None)
        if not stored_session_matches(probe, self._auth):
            raise NativeOnboardingError(
                "A sessão nativa não corresponde à autenticação preparada. Entre novamente.")
        return deepcopy(bundle)

    def complete(self, native_token, farmax=None, folder_to_watch=None, token_bundle=None):
        """Persist the independent grant, verify exact target, atomically activate.

        Failed/cancelled attempts retain their UUID and independent token cache.
        There is deliberately no fallible write after config replacement.
        """
        from config import Settings
        from utils.token_storage import read_token_from_file, store_token_at_file
        with self._lock:
            self._assert_current()
            self._principal(native_token)
            target = self._state.get("target")
            request = self._state.get("request")
            if target is None or request is None:
                raise NativeOnboardingError("Prepare uma conexão antes de concluir.")
            self._validate_target(target, request)
            source_fields = validate_source_setup(
                request["source"], target["api"]["native"]["timezone"],
                farmax=farmax, folder_to_watch=folder_to_watch)
            bundle = deepcopy(token_bundle) if token_bundle is not None else read_token_from_file(self.token_path)
            bundle = self._validated_bundle(bundle, native_token)
            proposed = deepcopy(self._document)
            proposed.pop("onboarding", None)
            proposed.update(
                auth=deepcopy(target["auth"]), api=deepcopy(target["api"]),
                target_system=request["source"], sqlite_path=str(self.database_path),
                token_path=str(self.token_path), native_recovery_required=True,
                installation_id=self.installation_id,
                **source_fields)
            settings = Settings.model_validate(proposed)
            contents = yaml.safe_dump(proposed, allow_unicode=True, sort_keys=False).encode("utf-8")
            # Re-persist/read back even when AuthService wrote the cache: token
            # publication errors can never leave a committed native config.
            store_token_at_file(bundle, self.token_path)
            if read_token_from_file(self.token_path) != bundle:
                raise NativeOnboardingError("Não foi possível confirmar a sessão salva.")
            self._save_state(dict(
                self._state, prepared_config_sha256=hashlib.sha256(contents).hexdigest()))

            def verify_and_check():
                self._assert_current()
                native = target["api"]["native"]
                verified = self.transport.verify(
                    native_token, self.installation_id,
                    native["merchant_id"], native["connection_id"])
                if verified.get("authorized") is not True or verified.get("target") != target:
                    raise NativeOnboardingError(
                        "O acesso à conexão mudou. Atualize a autenticação antes de continuar.")
                self._assert_current()
                if read_token_from_file(self.token_path) != bundle:
                    raise NativeOnboardingError("A sessão mudou durante a configuração. Tente novamente.")

            # fsync the candidate first; verify is the last network operation,
            # immediately before the atomic config pointer replacement.
            _atomic_write(self.config_path, contents, before_replace=verify_and_check)
            return settings
