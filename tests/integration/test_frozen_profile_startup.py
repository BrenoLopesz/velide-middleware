"""Optional frozen GUI smoke: no API credentials, ERP connections or installers."""
import os
from pathlib import Path
import subprocess

import pytest
from pytestqt.exceptions import TimeoutError as QtTimeoutError

from backend_profiles import prepare_profile_state, select_backend
from local_upgrade import upgrade_installation
from test_automatic_upgrade import installed, package_for, supply, receipt


def test_frozen_application_keeps_legacy_offline_intake_and_native_recovery_gate(tmp_path, qtbot):
    executable = os.environ.get("VELIDE_PROFILE_APPLICATION_EXE")
    if not executable:
        pytest.skip("Set VELIDE_PROFILE_APPLICATION_EXE to the built main.exe")
    assert Path(executable).is_file()
    config, database, folder, _ = installed(tmp_path, "CDS", mappings=False)
    adopted = upgrade_installation(config, tmp_path)
    supply(config, package_for(config, database, adopted))
    upgraded = upgrade_installation(config, tmp_path)
    assert upgraded.native_applied
    # Native intake now deliberately waits for authenticated remote recovery.
    # Keep the legacy selector to observe durable startup without API credentials.
    selected = select_backend(config, tmp_path, "legacy")
    _, internal = receipt(folder, "ent-offline-after-upgrade.json")
    registry, _ = prepare_profile_state(selected, tmp_path)
    environment = dict(os.environ, VELIDE_CONFIG_PATH=str(config), QT_QPA_PLATFORM="offscreen")
    process = subprocess.Popen([executable, "--is-update-checked"], env=environment,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        try:
            # Cold frozen imports / endpoint protection can exceed a fixed
            # 25-second startup window. Observe the durable readiness event.
            qtbot.waitUntil(lambda: registry.owner(internal) == "legacy" or process.poll() is not None,
                            timeout=90_000)
        except QtTimeoutError:
            pass  # Report ownership and captured diagnostics after cleanup.
        running = process.poll() is None
    finally:
        if process.poll() is None:
            process.terminate()
        stdout, stderr = process.communicate(timeout=15)
    diagnostic = (stdout + stderr).decode("utf-8", errors="replace")
    assert running, diagnostic
    assert registry.owner(internal) == "legacy", diagnostic
