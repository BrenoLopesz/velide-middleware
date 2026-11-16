"""Profile provisioning and a reversible NEW-intake selector.

Only the automatic installer/CLI calls this module while holding the application
instance lock. Runtime selection changes the shared ledger and config, not orders.
"""
from copy import deepcopy
import json
import os
from pathlib import Path
import sqlite3
import tempfile

from filelock import FileLock
import jwt
import yaml

from api.backend_ownership import BackendOwnership, ProfileOwnership
from utils.source_identity import configured_scope, configured_source_identity


def absolute(path, bundle_dir):
    path = Path(path)
    return path.resolve() if path.is_absolute() else (Path(bundle_dir) / path).resolve()


def token_principal(token):
    """Local continuity hint only; the API must verify the token before binding."""
    try:
        claims = jwt.decode(token[7:] if token.startswith("Bearer ") else token,
                            options={"verify_signature": False, "verify_exp": False})
        issuer, subject = claims["iss"], claims["sub"]
        if not isinstance(issuer, str) or not issuer.startswith("https://") or not isinstance(subject, str) or not subject:
            raise ValueError()
        return json.dumps([issuer.rstrip("/") + "/", subject], separators=(",", ":"))
    except (jwt.InvalidTokenError, KeyError, TypeError, AttributeError, ValueError) as error:
        raise ValueError("Não foi possível identificar a conta autenticada.") from error


def cached_principal(path, auth):
    from models.exceptions import TokenStorageError
    from utils.token_storage import read_token_from_file
    from workers.stored_token_retriever_worker import stored_session_matches
    try:
        bundle = read_token_from_file(path)
        if bundle and stored_session_matches(bundle, auth):
            return token_principal(bundle["access_token"])
    except (ValueError, OSError, TokenStorageError):
        pass
    return None


def configurations(settings, bundle_dir):
    if settings.backend_profiles:
        profiles = {name: settings.for_backend(name) for name in settings.backend_profiles}
    else:
        name = "native" if settings.api.native else "legacy"
        profiles = {name: settings.model_copy(deep=True)}
    for name, profile in profiles.items():
        profile.active_backend = name
        profile.sqlite_path = str(absolute(profile.sqlite_path, bundle_dir))
        profile.token_path = str(absolute(profile.token_path or "resources/token.txt", bundle_dir))
    return profiles


def prepare_profile_state(settings, bundle_dir):
    """Adopt old mappings automatically; never claim ownership from ID shape."""
    profiles = configurations(settings, bundle_dir)
    selected = settings.active_backend if settings.backend_profiles else next(iter(profiles))
    path = absolute(settings.backend_ownership_path or (profiles[selected].sqlite_path + ".ownership.db"), bundle_dir)
    paths = [profile.sqlite_path for profile in profiles.values()] + [profile.token_path for profile in profiles.values()]
    normalized = [os.path.normcase(str(Path(value).resolve())) for value in paths]
    if len(set(normalized)) != len(normalized) or os.path.normcase(str(path)) in normalized:
        raise ValueError("Os perfis devem usar bancos, tokens e registro de propriedade separados.")
    identity = configured_source_identity(settings)
    if identity is None:
        raise ValueError("A origem de pedidos não está configurada.")
    if settings.backend_profiles and (not path.is_file() or path.stat().st_size == 0):
        raise ValueError("O registro de propriedade dos perfis está ausente. Restaure o backup; não recrie pedidos.")
    registry = BackendOwnership(path, identity)
    if settings.backend_profiles and settings.backend_ownership_id != registry.identity:
        raise ValueError("O registro de propriedade dos perfis foi substituído. Restaure o backup correspondente à configuração.")
    for name, profile in profiles.items():
        registry.register(name, configured_scope(profile), profile.sqlite_path)
        database = Path(profile.sqlite_path)
        if not database.is_file():
            if settings.backend_profiles:
                raise ValueError("O banco retido de uma conexão está ausente. Restaure o backup antes de sincronizar.")
            continue
        conn = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
        try:
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if settings.backend_profiles and not {"SyncOrders", "SyncScope", "DeliveryMapping"}.issubset(tables):
                raise ValueError("O banco de uma conexão perdeu seus dados de sincronização. Restaure o backup.")
            ids = set()
            if "SyncOrders" in tables:
                ids.update(row[0] for row in conn.execute("SELECT internal_id FROM SyncOrders"))
            if "DeliveryMapping" in tables:
                ids.update(row[0] for row in conn.execute("SELECT internal_delivery_id FROM DeliveryMapping"))
        finally:
            conn.close()
        if name == "legacy" and registry.recorded_ids(name) - ids:
            raise ValueError("O banco legado perdeu pedidos já registrados. Restaure o backup antes de reenviar.")
        registry.seed(name, sorted(ids), cached_principal(profile.token_path, profile.auth))
    registry.select(selected)
    return registry, profiles


def add_provisioned_profiles(document, proposed, bundle_dir, native_database):
    """Retain the actual installed legacy settings, including its token cache."""
    result = deepcopy(proposed)
    if document.get("backend_profiles"):
        raise ValueError("Uma segunda migração de perfis exige um pacote específico.")
    legacy = {"auth": deepcopy(document["auth"]), "api": deepcopy(document["api"]),
              "sqlite_path": str(absolute(document.get("sqlite_path", "resources/velide.db"), bundle_dir)),
              "token_path": str(absolute(document.get("token_path") or "resources/token.txt", bundle_dir))}
    if not legacy["api"].get("source_timezone"):
        # The package's authoritative source timezone gives both scanners the
        # same receipt instant. Frozen legacy requests keep their old timestamp.
        legacy["api"]["source_timezone"] = proposed["api"]["native"]["timezone"]
    native = {"auth": deepcopy(proposed["auth"]), "api": deepcopy(proposed["api"]),
              "sqlite_path": str(native_database), "token_path": str(native_database) + ".token.txt"}
    result.update(backend_profiles={"legacy": legacy, "native": native}, active_backend="native",
                  token_path=native["token_path"])
    return result


def select_backend(config_path, bundle_dir, name, registry=None, runtime_settings=None):
    """Runtime or stopped-installation CLI selection; never moves an order."""
    from config import Settings
    path = Path(config_path).resolve()
    with FileLock(str(path) + ".upgrade.lock", timeout=0):
        raw = path.read_bytes()
        document = yaml.safe_load(raw.decode("utf-8-sig"))
        settings = Settings.model_validate(document)
        if runtime_settings is not None and settings != runtime_settings:
            raise ValueError("A configuração foi alterada fora do aplicativo. Reinicie antes de selecionar uma conexão.")
        if name not in settings.backend_profiles:
            raise ValueError("A conexão solicitada ainda não foi provisionada.")
        if registry is None:
            registry, _ = prepare_profile_state(settings, bundle_dir)
        if registry.source_identity != configured_source_identity(settings):
            raise ValueError("A origem de pedidos foi alterada.")
        if registry.identity != settings.backend_ownership_id or Path(registry.path).resolve() != absolute(settings.backend_ownership_path, bundle_dir):
            raise ValueError("O registro de propriedade foi alterado.")
        selected = document["backend_profiles"][name]
        document.update(active_backend=name, auth=deepcopy(selected["auth"]), api=deepcopy(selected["api"]),
                        sqlite_path=selected["sqlite_path"], token_path=selected["token_path"])
        Settings.model_validate(document)

        def publish():
            descriptor, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".select-", suffix=".tmp")
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(yaml.safe_dump(document, allow_unicode=True, sort_keys=False).encode("utf-8"))
                    stream.flush()
                    os.fsync(stream.fileno())
                if path.read_bytes() != raw:
                    raise ValueError("A configuração foi alterada durante a seleção.")
                os.replace(temporary, str(path))
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)

        registry.select(name, publish=publish)
        return Settings.model_validate(document)


def ownership_for(registry, name):
    return ProfileOwnership(registry, name)
