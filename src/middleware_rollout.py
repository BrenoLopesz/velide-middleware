"""Headless automatic rollout coordinator. The GUI supplies a graph pause adapter.

The real inventory exporter and copy-on-write consumer remain authoritative.
Call recover_local_rollout before building ANY service graph. An interrupted
uncommitted attempt is always invalidated locally, including when offline.
"""
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import uuid

from filelock import FileLock
import yaml

from middleware_transport import MiddlewareError, has_explicit_grant_scope, sha256_text, validate_target


IN_PROGRESS = {"PAUSING", "WAITING", "READY", "APPLYING"}


def atomic_json(path, document):
    path = Path(path)
    fd, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(json.dumps(document, ensure_ascii=False, sort_keys=True).encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, str(path))
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def rollout_paths(config_path):
    path = Path(config_path).resolve()
    return (path.with_name(path.name + ".rollout.json"),
            path.with_name(path.name + ".automatic-native-upgrade.json"),
            path.with_name(path.name + ".prepared-native-token.txt"))


def read_state(config_path):
    path = rollout_paths(config_path)[0]
    if not path.exists():
        return {"version": 1, "state": "IDLE"}
    state = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(state, dict) or state.get("version") != 1:
        raise MiddlewareError("RECOVERY_REQUIRED", "Invalid rollout state; restore the installation backup")
    return state


def _target_for_settings(settings):
    api = settings.api
    return {"auth": settings.auth.model_dump(),
            "api": {"velide_server": api.velide_server, "velide_websockets_server": api.velide_websockets_server,
                    "use_ssl": api.use_ssl, "native": {key: getattr(api.native, key)
                    for key in ("merchant_id", "connection_id", "timezone")}}}


def committed_receipt(config_path, bundle_dir, state):
    """Check the existing consumer's DB receipt THROUGH the committed config."""
    if not state.get("consumer_identity"):
        return False
    from config import Settings
    from backend_profiles import absolute
    settings = Settings.from_yaml(str(config_path))
    if settings.onboarding:
        return False
    native = settings.for_backend("native") if "native" in settings.backend_profiles else settings
    if not native.api.native:
        return False
    if _target_for_settings(native) != state.get("target"):
        raise MiddlewareError("RECOVERY_REQUIRED", "Committed native target differs from the rollout receipt")
    database = absolute(native.sqlite_path, bundle_dir)
    conn = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
    try:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='UpgradePackages'").fetchone():
            return False
        return bool(conn.execute("SELECT 1 FROM UpgradePackages WHERE identity=?",
                                 (state["consumer_identity"],)).fetchone())
    finally:
        conn.close()


def recover_local_rollout(config_path, bundle_dir=None):
    """Offline startup barrier. Returns persisted status; never applies a package."""
    path = Path(config_path).resolve()
    root = Path(bundle_dir).resolve() if bundle_dir else path.parent.parent
    state_path, package_path, prepared_path = rollout_paths(path)
    state = read_state(path)
    if state.get("attempt_id") and committed_receipt(path, root, state):
        state["state"] = "APPLIED" if state.get("state") == "APPLIED" else "ACK_PENDING"
        atomic_json(state_path, state)
        if package_path.exists():
            package_path.unlink()
        if prepared_path.exists():
            prepared_path.unlink()
    elif state.get("state") in IN_PROGRESS:
        state.update(state="INVALIDATED", cancel_reason="restart", cancel_pending=True)
        # Invalidation MUST commit before legacy writers can resume.
        atomic_json(state_path, state)
        if package_path.exists():
            os.replace(str(package_path), str(package_path) + ".invalidated")
    elif state.get("state") in {"ACK_PENDING", "APPLIED"}:
        raise MiddlewareError("RECOVERY_REQUIRED", "Native commit receipt is missing; restore its database/configuration")
    elif package_path.exists():
        os.replace(str(package_path), str(package_path) + ".invalidated")
    return state


class RolloutCoordinator:
    """One live attempt per instance; caller holds the application instance lock.

    transport: MiddlewareTransport; bootstrap_token/native token strings or callables.
    graph: quiesce(deadline), resume(), release() adapter (Qt or headless).
    All methods are synchronous; no sleeping or polling loop is hidden here.
    """
    def __init__(self, config_path, bundle_dir, transport, bootstrap_token, graph,
                 clock=time.monotonic, wall_clock=time.time, notify=None):
        self.config_path = Path(config_path).resolve()
        self.bundle_dir = Path(bundle_dir).resolve()
        self.transport, self.bootstrap_token, self.graph = transport, bootstrap_token, graph
        self.clock, self.wall_clock, self.notify = clock, wall_clock, notify or (lambda state: None)
        self.state_path, self.package_path, self.prepared_path = rollout_paths(self.config_path)
        self.state = recover_local_rollout(self.config_path, self.bundle_dir)
        self.deadline, self._locks, self._paused = None, None, False
        self._native_token = None

    def _save(self, state=None, **changes):
        candidate = deepcopy(self.state)
        if state is not None:
            candidate["state"] = state
        candidate.update(changes)
        atomic_json(self.state_path, candidate)
        self.state = candidate
        self.notify(deepcopy(self.state))

    @staticmethod
    def _token(value):
        return value() if callable(value) else value

    def _bootstrap(self):
        token = self._token(self.bootstrap_token)
        if not token:
            raise MiddlewareError("LOGIN_REQUIRED", "Entre na conexão legada para verificar a atualização", True)
        return token

    def discover(self):
        if self.state["state"] in {"ACK_PENDING", "APPLIED"}:
            return self.state
        if self.state["state"] in IN_PROGRESS:
            raise MiddlewareError("ATTEMPT_ACTIVE", "A final inventory attempt is already active")
        from config import Settings
        from local_upgrade import installation_binding
        from backend_profiles import absolute
        settings = Settings.from_yaml(str(self.config_path))
        if settings.onboarding or settings.api.native or settings.backend_profiles:
            return self.state
        binding = installation_binding(self.config_path, absolute(settings.sqlite_path, self.bundle_dir))
        self._retry_cancel()
        result = self.transport.discover(self._bootstrap(), binding["installation_id"],
                                         settings.target_system.value, binding["source_identity"])
        target = result.get("target")
        if result["state"] == "ENABLED" and target is None:
            raise MiddlewareError("INVALID_TARGET", "Enabled rollout has no target")
        self._save("AWAITING_LOGIN" if result["state"] == "ENABLED" else result["state"],
                   installation_id=binding["installation_id"], target=target,
                   retry_after_seconds=result.get("retry_after_seconds", 30))
        return result

    def _verify(self, token):
        native = self.state["target"]["api"]["native"]
        result = self.transport.verify(token, self.state["installation_id"],
                                       native["merchant_id"], native["connection_id"])
        if result["target"] != self.state["target"]:
            raise MiddlewareError("TARGET_CHANGED", "Native target changed; discover and authenticate again")

    def prepare(self, bundle):
        """Accept ONLY the independent native device-flow bundle, not legacy cache."""
        from config import AuthenticationConfig, Settings
        from backend_profiles import absolute
        from utils.token_storage import read_token_from_file, store_token_at_file
        from workers.stored_token_retriever_worker import auth_context, stored_session_matches
        if self.state["state"] not in {"AWAITING_LOGIN", "PREPARED"}:
            raise MiddlewareError("NOT_ENABLED", "Discover an enabled rollout before native login")
        validate_target(self.state["target"], self.transport.allow_loopback)
        auth = AuthenticationConfig.model_validate(self.state["target"]["auth"])
        # Check token claims even if a caller supplied a matching metadata label.
        probe = dict(bundle)
        probe.pop("middleware_auth", None)
        if (not has_explicit_grant_scope(probe) or not stored_session_matches(probe, auth)
                or not isinstance(bundle.get("refresh_token"), str) or not bundle["refresh_token"]):
            raise MiddlewareError("TOKEN_BINDING", "Native grant does not match the proposed authentication configuration")
        token = bundle.get("access_token")
        self._verify(token)  # Signature/expiry/permissions checked by the server.
        settings = Settings.from_yaml(str(self.config_path))
        legacy = read_token_from_file(absolute(settings.token_path or "resources/token.txt", self.bundle_dir))
        if legacy and (legacy.get("access_token") == token or (bundle.get("refresh_token") and
                       legacy.get("refresh_token") == bundle["refresh_token"])):
            raise MiddlewareError("INDEPENDENT_LOGIN_REQUIRED", "Prepare a separate native device login")
        prepared = dict(bundle, middleware_auth=auth_context(auth), middleware_rollout_target=self.state["target"])
        store_token_at_file(prepared, self.prepared_path)
        self._native_token = token
        self._save("PREPARED")

    def _prepared(self):
        from utils.token_storage import read_token_from_file
        bundle = read_token_from_file(self.prepared_path)
        if not bundle or bundle.get("middleware_rollout_target") != self.state["target"]:
            raise MiddlewareError("LOGIN_REQUIRED", "Prepare native login again", True)
        return bundle

    def _check_deadline(self):
        if self.state["state"] == "INVALIDATED":
            raise MiddlewareError("CANCELLED", "Final inventory attempt was invalidated")
        if self.deadline is None or self.clock() >= self.deadline:
            raise MiddlewareError("TIMEOUT", "Final inventory window expired; legacy synchronization will resume", True)
        self.transport.deadline = time.monotonic() + max(0, self.deadline - self.clock())

    def begin(self, pause_seconds=120):
        if self.state["state"] != "PREPARED" or not 15 <= pause_seconds <= 300:
            raise MiddlewareError("NOT_PREPARED", "Prepare native login before the final inventory")
        bundle = self._prepared()
        self._verify(bundle["access_token"])  # Immediately BEFORE pausing writers.
        self.deadline = self.clock() + pause_seconds
        self._save("PAUSING", attempt_id=str(uuid.uuid4()), receipt_id=str(uuid.uuid4()),
                   package_sha256=None, consumer_identity=None, cancel_pending=False)
        try:
            self._paused = True
            if self.graph.quiesce(self.deadline) is False:
                raise MiddlewareError("QUIESCENCE_FAILED", getattr(self.graph, "last_error", None)
                                      or "Service writers did not drain before the deadline", True)
            self._check_deadline()
            # Shared FileLock instance is reentrant for exporter and consumer.
            # The graph adapter holds journal/source locks after its drain.
            self._locks = ExitStack()
            self._locks.enter_context(FileLock(str(self.config_path) + ".rollout.lock", timeout=0))
            self._locks.enter_context(FileLock(str(self.config_path) + ".upgrade.lock", timeout=0, is_singleton=True))
            from upgrade_inventory import export_upgrade_inventory
            inventory = export_upgrade_inventory(self.config_path, self.bundle_dir)
            if inventory["pending_effects"]:
                raise MiddlewareError("ERP_EFFECTS_PENDING", "Aguardando confirmação das alterações pendentes no ERP", True)
            if inventory["binding"]["installation_id"] != self.state["installation_id"]:
                raise MiddlewareError("INSTALLATION_CHANGED", "Installed source changed during preparation")
            inventory_json = json.dumps(inventory, ensure_ascii=False, separators=(",", ":"))
            self._save("WAITING", inventory_sha256=sha256_text(inventory_json),
                       inventory_binding=inventory["binding"], inventory_state_sha256=inventory["state_sha256"])
            self._check_deadline()
            # Server window is bounded by REMAINING local pause, never extended.
            remaining = min(pause_seconds, int(self.deadline - self.clock()))
            if remaining < 15:
                raise MiddlewareError("TIMEOUT", "Insufficient time for final inventory", True)
            status = self.transport.submit_inventory(self._bootstrap(), self.state["installation_id"],
                                                       self.state["attempt_id"], inventory_json, remaining)
            return self._accept_status(status)
        except BaseException:
            self._abort_after_error()
            raise

    def _accept_status(self, status):
        self._check_deadline()
        if (status.get("attempt_id") != self.state["attempt_id"] or
                status.get("inventory_sha256") != self.state["inventory_sha256"]):
            raise MiddlewareError("ATTEMPT_BINDING", "Server returned another inventory attempt")
        if status.get("state") not in {"WAITING", "READY"}:
            raise MiddlewareError("ATTEMPT_INVALID", "Final inventory is no longer current; retry with a new snapshot", True)
        try:
            expiry = datetime.fromisoformat(status["expires_at"].replace("Z", "+00:00"))
            if expiry.tzinfo is None or expiry.timestamp() <= self.wall_clock():
                raise ValueError()
        except (ValueError, KeyError, TypeError):
            raise MiddlewareError("ATTEMPT_EXPIRED", "Server inventory attempt expired", True)
        self._save(status["state"], package_sha256=status.get("package_sha256"),
                   retry_after_seconds=max(1, min(10, status.get("retry_after_seconds", 2))))
        return status

    def poll(self):
        if self.state["state"] not in {"WAITING", "READY"}:
            raise MiddlewareError("NO_LIVE_ATTEMPT", "No live final inventory attempt")
        try:
            self._check_deadline()
            status = self._accept_status(self.transport.status(self._bootstrap(), self.state["installation_id"], self.state["attempt_id"]))
            if status["state"] == "READY":
                return self.apply()
            return status
        except BaseException:
            self._abort_after_error()
            raise

    def apply(self):
        self._check_deadline()
        if self.state["state"] != "READY" or not self._paused:
            raise MiddlewareError("NO_LIVE_ATTEMPT", "Package application requires the paused live attempt")
        try:
            envelope = self.transport.package(self._bootstrap(), self.state["installation_id"], self.state["attempt_id"])
            self._check_deadline()
            for key in ("attempt_id", "installation_id", "inventory_sha256", "package_sha256"):
                if not self.state.get(key) or envelope.get(key) != self.state[key]:
                    raise MiddlewareError("PACKAGE_BINDING", "Package does not match the live inventory attempt")
            text = envelope["package_json"]
            if sha256_text(text) != envelope["package_sha256"]:
                raise MiddlewareError("PACKAGE_DIGEST", "Package UTF-8 digest does not match")
            package = json.loads(text)
            if (package.get("binding") != self.state["inventory_binding"] or
                    package.get("inventory_state_sha256") != self.state["inventory_state_sha256"]):
                raise MiddlewareError("PACKAGE_BINDING", "Package does not match the final installed state")
            # The exporter may omit use_ssl (legacy schema); the effective
            # configuration is checked against every target field before commit.
            from config import Settings, native_upgrade_document
            document = yaml.safe_load(self.config_path.read_text(encoding="utf-8-sig"))
            proposed = Settings.model_validate(native_upgrade_document(document, package["configuration"], "unused.db"))
            if _target_for_settings(proposed) != self.state["target"]:
                raise MiddlewareError("TARGET_CHANGED", "Package target differs from the verified native connection")
            consumer_identity = hashlib.sha256(json.dumps(package, sort_keys=True).encode("utf-8")).hexdigest()
            self._save("APPLYING", consumer_identity=consumer_identity)
            # Publish EXACT downloaded bytes under an owned non-generic filename.
            fd, staging = tempfile.mkstemp(dir=str(self.package_path.parent), prefix=self.package_path.name, suffix=".tmp")
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(text.encode("utf-8"))
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(staging, str(self.package_path))
            finally:
                if os.path.exists(staging):
                    os.unlink(staging)
            bundle = self._prepared()
            from utils.token_storage import store_token_at_file
            def before_commit(settings):
                self._check_deadline()
                if _target_for_settings(settings.for_backend("native")) != self.state["target"]:
                    raise MiddlewareError("TARGET_CHANGED", "Consumer native target changed")
                # Independent native grant only; never duplicate legacy refresh.
                store_token_at_file(bundle, settings.for_backend("native").token_path)
                self._check_deadline()
            from local_upgrade import upgrade_installation
            result = upgrade_installation(self.config_path, self.bundle_dir, self.package_path,
                                          before_commit=before_commit, automatic=True)
            if result.package_error or not committed_receipt(self.config_path, self.bundle_dir, self.state):
                raise MiddlewareError("PACKAGE_REJECTED", result.package_error or "Consumer did not commit")
            self._save("ACK_PENDING")
            self._release()
            # The caller rebuilds/restarts from committed config, NEVER resumes
            # this legacy graph after any successful local cutover.
            if self.prepared_path.exists():
                self.prepared_path.unlink()
            if self.package_path.exists():
                self.package_path.unlink()
            try:
                self.acknowledge(bundle["access_token"])
            except MiddlewareError:
                pass  # Durable receipt is retried at startup, native stays active.
            return result
        except BaseException:
            self._abort_after_error()
            raise

    def _release(self):
        self.transport.deadline = None
        if self._locks:
            self._locks.close()
            self._locks = None
        self.graph.release()

    def _abort_after_error(self):
        # A token/disk/ack failure AFTER config replace must never resume legacy.
        if committed_receipt(self.config_path, self.bundle_dir, self.state):
            try:
                self._save("ACK_PENDING")
            finally:
                # APPLYING was already durable before config replacement, so
                # startup can recover the consumer receipt even on a full disk.
                self._release()
            return
        if self.state["state"] == "INVALIDATED":
            self._retry_cancel()
            return
        reason = "timeout" if self.deadline is not None and self.clock() >= self.deadline else "failed"
        self.cancel(reason)

    def request_cancel(self, reason="cancelled"):
        """Local-only cancellation, safe from a nested GUI drain event.

        Do not perform network I/O here: the interrupted operation must unwind
        before retrying server cancellation. Late worker outcomes still drain.
        """
        if self.state["state"] in {"ACK_PENDING", "APPLIED"} or committed_receipt(self.config_path, self.bundle_dir, self.state):
            raise MiddlewareError("ALREADY_COMMITTED", "Native activation is committed; retry acknowledgement")
        self._save("INVALIDATED", cancel_reason=reason, cancel_pending=bool(self.state.get("attempt_id")))
        if self.package_path.exists():
            os.replace(str(self.package_path), str(self.package_path) + ".invalidated")
        self._release()
        if self._paused:
            self.graph.resume()
            self._paused = False
        self.deadline = None
        return self.state

    def cancel(self, reason="cancelled"):
        self.request_cancel(reason)
        self._retry_cancel()
        return self.state

    def _retry_cancel(self):
        if not self.state.get("cancel_pending"):
            return
        try:
            self.transport.cancel(self._bootstrap(), self.state["installation_id"],
                                  self.state["attempt_id"], self.state.get("cancel_reason", "restart"))
            self._save(cancel_pending=False)
        except MiddlewareError:
            pass  # Local durable invalidation is authoritative even offline.

    def acknowledge(self, native_token=None):
        if self.state["state"] == "APPLIED":
            return self.state
        if not committed_receipt(self.config_path, self.bundle_dir, self.state):
            raise MiddlewareError("NO_COMMIT", "No committed native application receipt")
        token = self._token(native_token) if native_token is not None else self._bootstrap()
        status = self.transport.ack(token, self.state["installation_id"], self.state["attempt_id"],
                                    self.state["package_sha256"], self.state["receipt_id"])
        if status.get("state") != "APPLIED" or any(
                status.get(key) != self.state[key] for key in ("attempt_id", "inventory_sha256", "package_sha256")):
            raise MiddlewareError("ACK_CONFLICT", "Server did not acknowledge the committed native receipt")
        self._save("APPLIED", cancel_pending=False)
        return status
