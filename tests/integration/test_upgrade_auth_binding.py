"""Exercise cutover-produced cache metadata through the real token reader/signals."""
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import jwt
import pytest

from local_upgrade import upgrade_installation
from test_automatic_upgrade import installed, package_for, supply
import upgrade_auth
from workers.stored_token_retriever_worker import StoredTokenRetrieverWorker, auth_context, stored_session_matches


def legacy_bundle(auth, expired=False):
    # Deliberately omit scope from both the original JWT and the token bundle.
    token = jwt.encode({"iss": "https://" + auth.domain + "/", "aud": [auth.audience, "userinfo"],
                        "azp": auth.client_id, "exp": time.time() + (-100 if expired else 3600)},
                       "test-only-key", algorithm="HS256")
    return {"access_token": token, "refresh_token": "original-refresh-token", "token_type": "Bearer", "extra": {"keep": True}}


def write_bundle(path, bundle, wrapped=False):
    encoded = base64.b64encode(json.dumps(bundle, indent=2).encode("utf-8"))
    if wrapped:
        encoded = b"\r\n".join(encoded[index:index + 64] for index in range(0, len(encoded), 64)) + b"\r\n"
    path.write_bytes(encoded)
    return encoded


def read_bundle(path):
    return json.loads(base64.b64decode(path.read_bytes()).decode("utf-8"))


def worker_signals(auth):
    worker = StoredTokenRetrieverWorker(auth)
    result = {"token": [], "expired": [], "error": [], "finished": []}
    worker.signals.token.connect(lambda *values: result["token"].append(values))
    worker.signals.expired.connect(result["expired"].append)
    worker.signals.error.connect(result["error"].append)
    worker.signals.finished.connect(lambda: result["finished"].append(True))
    worker.run()  # Real reader opens resources/token.txt; no stubbed eligibility.
    assert result["finished"] == [True]
    return result


def assert_old_session_usable(auth, bundle, expired=False):
    signals = worker_signals(auth)
    assert not signals["error"]
    assert signals["token"] == ([] if expired else [(bundle["access_token"], bundle["refresh_token"])])
    assert signals["expired"] == ([bundle["refresh_token"]] if expired else [])


def installation_with_package(tmp_path, monkeypatch):
    config, database, _, _ = installed(tmp_path)
    adopted = upgrade_installation(config, tmp_path)
    monkeypatch.setattr("utils.token_storage.BUNDLE_DIR", str(tmp_path))
    package = package_for(config, database, adopted)
    changed = auth_context(adopted.settings.auth)
    changed["scope"] += " integration.read"
    package["configuration"]["auth"] = changed
    supply(config, package)
    return config, adopted, package


@pytest.mark.parametrize("expired", [False, True])
@pytest.mark.parametrize("changed", ["scope", "domain", "audience", "client_id"])
def test_native_package_binds_legacy_cache_to_old_context_before_worker_can_publish_or_refresh(tmp_path, qtbot, monkeypatch, expired, changed):
    config, adopted, package = installation_with_package(tmp_path, monkeypatch)
    old_auth = adopted.settings.auth
    new_context = auth_context(old_auth)
    new_context[changed] = new_context[changed] + "-changed"
    package["configuration"]["auth"] = new_context
    supply(config, package)
    bundle = legacy_bundle(old_auth, expired)
    token_file = config.with_name("token.txt")
    original = write_bundle(token_file, bundle, wrapped=True)
    assert_old_session_usable(old_auth, bundle, expired)
    # Prove the reviewer case: absent legacy metadata cannot detect scope-only
    # change from iss/aud/azp. The cutover must supply its known OLD context.
    assert stored_session_matches(bundle, old_auth.model_copy(update={changed: new_context[changed]})) == (changed == "scope")
    result = upgrade_installation(config, tmp_path)
    assert result.native_applied, result.package_error
    bound = read_bundle(token_file)
    assert bound.pop("middleware_auth") == auth_context(old_auth)
    assert bound == bundle  # Access/refresh tokens and unknown bundle fields survive.
    backups = [Path(path) for path in result.backups if ".pre-native-auth-" in path]
    assert len(backups) == 1 and backups[0].read_bytes() == original
    signals = worker_signals(result.settings.auth)
    assert not signals["token"] and not signals["expired"]
    assert signals["error"] == ["A conexão foi atualizada. Entre novamente para continuar usando o Velide."]
    assert_old_session_usable(old_auth, bundle, expired)


@pytest.mark.parametrize("expired", [False, True])
def test_ordinary_upgrade_keeps_unbound_original_jwt_without_scope_eligible_and_unchanged(tmp_path, qtbot, monkeypatch, expired):
    from config import Settings
    config, _, _, _ = installed(tmp_path)
    old = Settings.from_yaml(str(config))
    token_file = config.with_name("token.txt")
    bundle = legacy_bundle(old.auth, expired)
    original = write_bundle(token_file, bundle)
    monkeypatch.setattr("utils.token_storage.BUNDLE_DIR", str(tmp_path))
    result = upgrade_installation(config, tmp_path)
    assert result.adopted and not result.native_applied and not result.package_error
    assert token_file.read_bytes() == original
    assert "middleware_auth" not in read_bundle(token_file)
    assert not list(token_file.parent.glob("token.txt.pre-native-auth-*"))
    assert_old_session_usable(result.settings.auth, bundle, expired)


@pytest.mark.parametrize("expired", [False, True])
def test_external_config_binds_the_actual_application_token_cache(tmp_path, qtbot, monkeypatch, expired):
    root = tmp_path / "application"
    root.mkdir()
    config, database, _, _ = installed(root)
    external = tmp_path / "separate-config"
    external.mkdir()
    config = external / "settings.yml"
    config.write_bytes((root / "resources" / "config.yml").read_bytes())
    adopted = upgrade_installation(config, root)
    package = package_for(config, database, adopted)
    package["configuration"]["auth"] = dict(auth_context(adopted.settings.auth), scope=adopted.settings.auth.scope + " integration.read")
    supply(config, package)
    token_file = root / "resources" / "token.txt"
    write_bundle(token_file, legacy_bundle(adopted.settings.auth, expired))
    monkeypatch.setattr("utils.token_storage.BUNDLE_DIR", str(root))
    result = upgrade_installation(config, root)
    assert result.native_applied, result.package_error
    assert not config.with_name("token.txt").exists()
    assert read_bundle(token_file)["middleware_auth"] == auth_context(adopted.settings.auth)
    signals = worker_signals(result.settings.auth)
    assert not signals["token"] and not signals["expired"] and signals["error"]


@pytest.mark.parametrize("failure", ["backup", "write", "replace"])
def test_token_preparation_failure_never_publishes_new_config_or_breaks_old_session(tmp_path, qtbot, monkeypatch, failure):
    config, adopted, _ = installation_with_package(tmp_path, monkeypatch)
    token_file = config.with_name("token.txt")
    bundle = legacy_bundle(adopted.settings.auth)
    original = write_bundle(token_file, bundle)
    original_config = config.read_bytes()
    real_write = upgrade_auth._write_durable
    real_replace = upgrade_auth.os.replace

    def fail_write(path, contents):
        if (failure == "backup" and ".pre-native-auth-" in path.name) or (failure == "write" and ".bind-auth-" in path.name):
            raise OSError("simulated token preparation failure")
        real_write(path, contents)

    def fail_replace(source, destination):
        if failure == "replace" and Path(destination) == token_file:
            raise OSError("simulated token preparation failure")
        return real_replace(source, destination)

    monkeypatch.setattr(upgrade_auth, "_write_durable", fail_write)
    monkeypatch.setattr(upgrade_auth.os, "replace", fail_replace)
    result = upgrade_installation(config, tmp_path)
    assert not result.native_applied and "simulated token preparation failure" in result.package_error
    assert config.read_bytes() == original_config and token_file.read_bytes() == original
    assert not list(token_file.parent.glob("token.txt.bind-auth-*.tmp"))
    assert_old_session_usable(result.settings.auth, bundle)


@pytest.mark.parametrize("expired", [False, True])
def test_failed_config_publication_keeps_old_context_binding_usable_and_retry_is_safe(tmp_path, qtbot, monkeypatch, expired):
    config, adopted, _ = installation_with_package(tmp_path, monkeypatch)
    token_file = config.with_name("token.txt")
    bundle = legacy_bundle(adopted.settings.auth, expired)
    write_bundle(token_file, bundle)
    original_config = config.read_bytes()
    real_replace = upgrade_auth.os.replace

    def fail_config(source, destination):
        if Path(destination) == config:
            # The token binding MUST already be durable at the activation point.
            assert read_bundle(token_file)["middleware_auth"] == auth_context(adopted.settings.auth)
            raise OSError("simulated config publication failure")
        return real_replace(source, destination)

    monkeypatch.setattr(upgrade_auth.os, "replace", fail_config)
    failed = upgrade_installation(config, tmp_path)
    assert not failed.native_applied and "config publication failure" in failed.package_error
    assert config.read_bytes() == original_config
    assert_old_session_usable(failed.settings.auth, bundle, expired)
    monkeypatch.setattr(upgrade_auth.os, "replace", real_replace)
    retried = upgrade_installation(config, tmp_path)
    assert retried.native_applied, retried.package_error
    assert len(list(token_file.parent.glob("token.txt.pre-native-auth-*.bak"))) == 1
    signals = worker_signals(retried.settings.auth)
    assert signals["error"] and not signals["token"] and not signals["expired"]
    # A later real native login must not be relabelled by replaying the package.
    native_bundle = dict(legacy_bundle(retried.settings.auth), middleware_auth=auth_context(retried.settings.auth))
    native_bytes = write_bundle(token_file, native_bundle)
    repeated = upgrade_installation(config, tmp_path)
    assert not repeated.native_applied and not repeated.package_error
    assert token_file.read_bytes() == native_bytes
    assert_old_session_usable(repeated.settings.auth, native_bundle)


def test_native_cutover_with_unchanged_auth_preserves_session_eligibility(tmp_path, qtbot, monkeypatch):
    config, adopted, package = installation_with_package(tmp_path, monkeypatch)
    del package["configuration"]["auth"]
    supply(config, package)
    bundle = legacy_bundle(adopted.settings.auth)
    write_bundle(config.with_name("token.txt"), bundle)
    result = upgrade_installation(config, tmp_path)
    assert result.native_applied, result.package_error
    assert_old_session_usable(result.settings.auth, bundle)


@pytest.mark.parametrize("content", [b"not-base64", base64.b64encode(b"[]"), base64.b64encode(b"{}")])
def test_malformed_session_aborts_cutover_without_rewriting_existing_files(tmp_path, monkeypatch, content):
    config, _, _ = installation_with_package(tmp_path, monkeypatch)
    token_file = config.with_name("token.txt")
    token_file.write_bytes(content)
    original_config = config.read_bytes()
    result = upgrade_installation(config, tmp_path)
    assert result.package_error and not result.native_applied
    assert config.read_bytes() == original_config and token_file.read_bytes() == content


def test_existing_foreign_auth_metadata_is_never_relabelled_to_old_or_proposed_context(tmp_path, monkeypatch):
    config, adopted, _ = installation_with_package(tmp_path, monkeypatch)
    bundle = dict(legacy_bundle(adopted.settings.auth), middleware_auth=dict(auth_context(adopted.settings.auth), client_id="other-client"))
    token_file = config.with_name("token.txt")
    original = write_bundle(token_file, bundle)
    original_config = config.read_bytes()
    result = upgrade_installation(config, tmp_path)
    assert "different authentication context" in result.package_error
    assert not result.native_applied
    assert config.read_bytes() == original_config and token_file.read_bytes() == original


def test_unbound_foreign_jwt_is_not_blessed_with_the_installed_auth_metadata(tmp_path, qtbot, monkeypatch):
    config, adopted, _ = installation_with_package(tmp_path, monkeypatch)
    foreign_auth = adopted.settings.auth.model_copy(update={"client_id": "different-issuer-client"})
    token_file = config.with_name("token.txt")
    original = write_bundle(token_file, legacy_bundle(foreign_auth))
    original_config = config.read_bytes()
    assert worker_signals(adopted.settings.auth)["error"]
    result = upgrade_installation(config, tmp_path)
    assert not result.native_applied and "pre-cutover authentication context" in result.package_error
    assert config.read_bytes() == original_config and token_file.read_bytes() == original
    assert worker_signals(adopted.settings.auth)["error"]


def test_headless_automatic_cli_binds_scope_only_cutover_cache_before_activation(tmp_path, qtbot, monkeypatch):
    from config import Settings

    config, adopted, _ = installation_with_package(tmp_path, monkeypatch)
    token_file = config.with_name("token.txt")
    write_bundle(token_file, legacy_bundle(adopted.settings.auth))
    script = Path(__file__).resolve().parents[2] / "src" / "native_migration.py"
    process = subprocess.run([sys.executable, str(script), "--automatic", "--config", str(config),
                              "--bundle-dir", str(tmp_path)], capture_output=True, text=True,
                             env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
    assert process.returncode == 0, process.stderr
    assert json.loads(process.stdout)["native_applied"], process.stdout
    assert read_bundle(token_file)["middleware_auth"] == auth_context(adopted.settings.auth)
    signals = worker_signals(Settings.from_yaml(str(config)).auth)
    assert signals["error"] and not signals["token"] and not signals["expired"]
