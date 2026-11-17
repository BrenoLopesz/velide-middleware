import json
from pathlib import Path
import subprocess
import sys
import time

import jwt
import pytest
import yaml

from backend_profiles import prepare_profile_state, select_backend, token_principal
from config import Settings
from local_upgrade import upgrade_installation
from test_automatic_upgrade import installed, package_for, supply, rows
from utils.token_storage import store_token_at_file, read_token_from_file


def cache(config, subject="original-user"):
    token = jwt.encode({"iss": "https://existing.invalid/", "aud": "installed-audience",
                        "azp": "installed-client", "sub": subject, "exp": int(time.time()) + 3600},
                       "disposable-test-key", algorithm="HS256")
    store_token_at_file({"access_token": token, "refresh_token": "original-refresh"}, config.with_name("token.txt"))
    return token


def provisioned(tmp_path, changed_auth=False):
    config, database, folder, original = installed(tmp_path)
    token = cache(config)
    adopted = upgrade_installation(config, tmp_path)
    package = package_for(config, database, adopted)
    if changed_auth:
        package["configuration"]["auth"] = {"domain": "new-tenant.invalid", "audience": "new-audience",
                                             "client_id": "new-client", "scope": "openid offline_access"}
    supply(config, package)
    result = upgrade_installation(config, tmp_path)
    assert not result.package_error
    return config, database, result, original, token


def test_package_provisions_both_profiles_without_user_config_edits(tmp_path):
    config, old_database, result, original, token = provisioned(tmp_path)
    assert result.settings.active_backend == "native"
    registry, profiles = prepare_profile_state(result.settings, tmp_path)
    assert set(profiles) == {"legacy", "native"}
    assert profiles["legacy"].api.velide_server == original["api"]["velide_server"]
    assert Path(profiles["legacy"].sqlite_path) == old_database
    assert Path(profiles["native"].sqlite_path) == Path(result.database_path)
    assert profiles["legacy"].token_path != profiles["native"].token_path
    assert read_token_from_file(profiles["legacy"].token_path)["access_token"] == token
    assert read_token_from_file(profiles["native"].token_path) is None
    assert registry.owner("123") == "native"  # Explicit authoritative migration.
    assert yaml.safe_load(config.read_text())["future_setting"] == original["future_setting"]


def test_changed_tenant_keeps_legacy_session_but_requires_native_login(tmp_path):
    _, _, result, _, token = provisioned(tmp_path, changed_auth=True)
    _, profiles = prepare_profile_state(result.settings, tmp_path)
    assert read_token_from_file(profiles["legacy"].token_path)["access_token"] == token
    assert read_token_from_file(profiles["native"].token_path) is None
    assert profiles["native"].auth.domain == "new-tenant.invalid"


@pytest.mark.parametrize("changed_auth", [False, True])
def test_cutover_never_forks_legacy_refresh_token_into_two_live_profiles(tmp_path, qtbot, changed_auth):
    from workers.stored_token_retriever_worker import StoredTokenRetrieverWorker
    _, _, result, _, _ = provisioned(tmp_path, changed_auth)
    _, profiles = prepare_profile_state(result.settings, tmp_path)
    legacy = read_token_from_file(profiles["legacy"].token_path)
    assert legacy["refresh_token"] == "original-refresh"
    native = StoredTokenRetrieverWorker(profiles["native"].auth, profiles["native"].token_path)
    refreshed, published, errors = [], [], []
    native.signals.expired.connect(refreshed.append)
    native.signals.token.connect(lambda *args: published.append(args))
    native.signals.error.connect(errors.append)
    native.run()
    assert errors and not refreshed and not published
    assert not Path(profiles["native"].token_path).exists()


def test_rollback_and_repeat_installer_do_not_reapply_migration_or_abandon_native(tmp_path):
    config, old_database, result, _, _ = provisioned(tmp_path)
    native_database = result.database_path
    selected = select_backend(config, tmp_path, "legacy")
    assert selected.active_backend == "legacy"
    assert Path(selected.sqlite_path) == old_database
    repeated = upgrade_installation(config, tmp_path)
    assert not repeated.package_error and not repeated.native_applied
    registry, profiles = prepare_profile_state(repeated.settings, tmp_path)
    assert registry.selected() == "legacy" and registry.owner("123") == "native"
    assert profiles["native"].sqlite_path == native_database
    assert rows(native_database, "SELECT external_delivery_id FROM DeliveryMapping")
    assert select_backend(config, tmp_path, "native").active_backend == "native"


def test_wrong_legacy_login_cannot_claim_retained_profile(tmp_path):
    config, _, result, _, token = provisioned(tmp_path)
    registry, _ = prepare_profile_state(result.settings, tmp_path)
    wrong = cache(config, "other-user")
    # Re-reading the cache on startup never reassigns the original principal.
    registry, _ = prepare_profile_state(result.settings, tmp_path)
    with pytest.raises(ValueError, match="conta original"):
        registry.bind_principal("legacy", token_principal(wrong))
    registry.bind_principal("legacy", token_principal(token))


def test_provisioning_failure_leaves_original_profile_and_owner(tmp_path, monkeypatch):
    config, database, _, _ = installed(tmp_path)
    cache(config)
    adopted = upgrade_installation(config, tmp_path)
    raw = config.read_bytes()
    supply(config, package_for(config, database, adopted))
    import local_upgrade
    original_replace = local_upgrade.os.replace
    def fail(source, destination):
        if Path(destination) == config:
            raise OSError("publication failed")
        return original_replace(source, destination)
    monkeypatch.setattr(local_upgrade.os, "replace", fail)
    result = upgrade_installation(config, tmp_path)
    assert result.package_error and config.read_bytes() == raw
    registry, profiles = prepare_profile_state(result.settings, tmp_path)
    assert registry.owner("123") == "legacy" and set(profiles) == {"legacy"}


def test_profile_paths_cannot_share_tokens_or_databases(tmp_path):
    _, _, result, _, _ = provisioned(tmp_path)
    document = result.settings.model_dump()
    document["backend_profiles"]["native"]["token_path"] = document["backend_profiles"]["legacy"]["token_path"]
    with pytest.raises(ValueError, match="separate"):
        Settings.model_validate(document)


def test_selection_cli_is_packaged_entrypoint_and_does_not_transfer_orders(tmp_path):
    import os
    config, _, result, _, _ = provisioned(tmp_path)
    executable = Path(__file__).parents[2] / "src" / "native_migration.py"
    command = [os.environ["VELIDE_UPGRADE_EXE"]] if os.environ.get("VELIDE_UPGRADE_EXE") else [sys.executable, str(executable)]
    completed = subprocess.run(command + ["--select-backend", "legacy",
                               "--config", str(config), "--bundle-dir", str(tmp_path)],
                              capture_output=True, text=True, check=True)
    assert json.loads(completed.stdout) == {"active_backend": "legacy", "existing_orders_transferred": False}
    selected = Settings.from_yaml(config)
    registry, _ = prepare_profile_state(selected, tmp_path)
    assert registry.owner("123") == "native"


def test_unprovisioned_native_selector_fails_without_touching_config(tmp_path):
    config, _, _, _ = installed(tmp_path)
    upgrade_installation(config, tmp_path)
    raw = config.read_bytes()
    with pytest.raises(ValueError, match="provisionada"):
        select_backend(config, tmp_path, "native")
    assert config.read_bytes() == raw


def test_missing_ownership_ledger_is_not_recreated_from_stale_profile_journals(tmp_path):
    _, _, result, _, _ = provisioned(tmp_path)
    Path(result.settings.backend_ownership_path).unlink()
    with pytest.raises(ValueError, match="propriedade.*ausente"):
        prepare_profile_state(result.settings, tmp_path)
    assert not Path(result.settings.backend_ownership_path).exists()


def test_valid_but_pre_cutover_ownership_ledger_cannot_replace_published_copy(tmp_path):
    import shutil
    config, old_database, result, _, _ = provisioned(tmp_path)
    published = Path(result.settings.backend_ownership_path)
    shutil.copyfile(str(old_database) + ".ownership.db", published)
    with pytest.raises(ValueError, match="substituído"):
        prepare_profile_state(result.settings, tmp_path)
    assert Settings.from_yaml(config).active_backend == "native"


def test_runtime_selection_rejects_external_config_changes(tmp_path):
    config, _, result, _, _ = provisioned(tmp_path)
    registry, _ = prepare_profile_state(result.settings, tmp_path)
    document = yaml.safe_load(config.read_text(encoding="utf-8"))
    document["backend_profiles"]["legacy"]["auth"]["client_id"] = "changed-outside-runtime"
    config.write_text(yaml.safe_dump(document), encoding="utf-8")
    with pytest.raises(ValueError, match="Reinicie"):
        select_backend(config, tmp_path, "legacy", registry=registry, runtime_settings=result.settings)
    assert registry.selected() == "native"


def test_profile_database_pointer_cannot_be_replaced_with_empty_database(tmp_path):
    _, _, result, _, _ = provisioned(tmp_path)
    original = result.settings.backend_profiles["legacy"].sqlite_path
    result.settings.backend_profiles["legacy"].sqlite_path = str(tmp_path / "replacement.db")
    with pytest.raises(ValueError, match="substituída"):
        prepare_profile_state(result.settings, tmp_path)
    assert Path(original).is_file()
