"""Small real Inno compile checks file selection without freezing/installing."""
import importlib.util
import os
from pathlib import Path
import re
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[2]
RESOURCES = ROOT / "installer" / "resources"
SPEC = importlib.util.spec_from_file_location("upgrade_release_resources", ROOT / "installer" / "src" / "cli" / "release_resources.py")
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)


def test_freezer_and_inno_asset_allowlists_agree_and_reject_local_data(tmp_path):
    text = (RESOURCES / "files_and_run.iss").read_text(encoding="utf-8")
    entries = re.findall(r'Source: "([^\"]+)"; DestDir: "([^\"]+)"', text)
    main_prefix = "..\\..\\middleware\\resources\\"
    main = {source[len(main_prefix):].replace("\\", "/") for source, _ in entries if source.startswith(main_prefix)}
    updater = {source.replace("\\", "/") for source, dest in entries if dest.startswith("{code:GetInstallerDestDir}\\resources")}
    assert main == set(release.MAIN_RESOURCES)
    assert updater == set(release.UPDATER_RESOURCES)
    for is_updater, root in ((False, ROOT / "resources"), (True, RESOURCES)):
        actual = release.resource_files(root, updater=is_updater)
        assert all(Path(source).is_file() for source, _ in actual)
        assert not any(dest.endswith(("velide.db", "token.txt", "native-upgrade.json")) for _, dest in actual)
        assert (str(root / "config.yml"), "resources/config.yml") in actual if is_updater else all(dest != "resources/config.yml" for _, dest in actual)
    for name in release.MAIN_RESOURCES:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fixture asset")
    for unwanted in ("velide.db", "test_config.yml", "token.txt", "native-upgrade.json", "customer-inventory.json"):
        (tmp_path / unwanted).write_bytes(b"private fixture")
    selected = {dest for _, dest in release.resource_files(tmp_path)}
    assert selected == {"resources/" + name for name in release.MAIN_RESOURCES}


def test_installer_sequence_runs_upgrade_for_silent_installs_before_startup_and_keeps_retry_path():
    text = (RESOURCES / "files_and_run.iss").read_text(encoding="utf-8").split("[Run]", 1)[1]
    assert text.index("vc_redist.") < text.index("migrate-native.exe") < text.index("{#MyAppExeName}")
    automatic = next(line for line in text.splitlines() if line.startswith("Filename:") and "migrate-native.exe" in line)
    assert "--automatic" in automatic and "waituntilterminated" in automatic
    assert "skipifsilent" not in automatic and "nowait" not in automatic
    logic = (RESOURCES / "logic.pas").read_text(encoding="utf-8")
    assert "function PrepareToInstall" in logic and "IsUpgrade and not InstalledConfigExists()" in logic
    batch = (RESOURCES / "apply_update.bat").read_text(encoding="utf-8")
    missing_source = batch.split('if not exist "%UPDATE_SOURCE_FOLDER%" (', 1)[1].split(")", 1)[0]
    failed_copy = batch.split("if %ERRORLEVEL% GEQ 8 (", 1)[1].split(")", 1)[0]
    assert "goto Restart" in missing_source and "goto Restart" in failed_copy
    assert 'set "MAIN_EXECUTABLE=main.exe"' in batch
    assert batch.index(":Restart") < batch.index('start "Restarting Application"')
    assert "--is-update-checked" in batch


def test_real_inno_compiler_keeps_runtime_dependencies_and_excludes_installation_and_test_data(tmp_path):
    compiler = Path(os.environ.get("VELIDE_ISCC", r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe"))
    if not compiler.is_file():
        pytest.skip("Inno Setup compiler is not available")
    middleware = tmp_path / "output" / "middleware"
    updater = tmp_path / "output" / "installer"
    script_dir = updater / "resources"
    output = tmp_path / "compiled"
    output.mkdir()
    for directory, assets in ((middleware / "resources", release.MAIN_RESOURCES), (script_dir, release.UPDATER_RESOURCES)):
        for name in assets:
            path = directory / name
            path.parent.mkdir(parents=True, exist_ok=True)
            # Compile the actual installer code/icon; payload bytes are deliberately tiny.
            path.write_bytes((RESOURCES / name).read_bytes() if name.endswith((".iss", ".pas", ".ico")) else b"fixture asset")
    required = [
        middleware / "main.exe", middleware / "migrate-native.exe", middleware / "sqlite3.dll",
        middleware / "lib" / "_sqlite3.pyd", middleware / "lib" / "library.zip",
        middleware / "lib" / "sqlite3" / "dbapi2.pyc",
        middleware / "lib" / "thirdparty" / "resources" / "nested-runtime-resource.bin",
        middleware / "lib" / "thirdparty" / "config.yml",
        middleware / "share" / "zoneinfo" / "runtime-tz-proof",
        middleware / "PyQt5.uic.widget-plugins" / "qaxcontainer.py",
        updater / "main.exe", updater / "lib" / "library.zip",
        script_dir / "vc_redist.x64.exe",
    ]
    forbidden = [
        middleware / "config.yml",
        middleware / "resources" / "config.yml", middleware / "resources" / "token.txt",
        middleware / "resources" / "velide.db", middleware / "resources" / "velide.db-wal",
        middleware / "resources" / "native-upgrade.json",
        middleware / "resources" / "must-not-ship-store-inventory.json",
        middleware / "tests" / "must-not-ship-test-data.json",
        middleware / "fixtures" / "must-not-ship-fixture.yml",
        updater / "resources" / "must-not-ship-updater-test-data.json",
    ]
    for path in required + forbidden:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fixture payload")
    process = subprocess.run([str(compiler), "/DMyArch=x64", "/DMyAppVersion=0.0.0", "/O" + str(output),
                              str(script_dir / "installer.iss")], capture_output=True, text=True, errors="replace")
    log = process.stdout + process.stderr
    assert process.returncode == 0, log
    for name in ("migrate-native.exe", "sqlite3.dll", "_sqlite3.pyd", "dbapi2.pyc", "library.zip",
                 "nested-runtime-resource.bin", "runtime-tz-proof", "qaxcontainer.py", "config_example.yml", "private_public.pem"):
        assert name.lower() in log.lower(), log
    for name in ("velide.db", "token.txt", "native-upgrade.json", "must-not-ship"):
        assert name not in log.lower(), log
    normalized = log.replace("/", "\\").lower()
    assert "middleware\\resources\\config.yml" not in normalized
    assert "installer\\resources\\config.yml" in normalized
    assert "thirdparty\\config.yml" in normalized
    artifacts = list(output.glob("*.exe"))
    assert artifacts  # Compiled only; never execute an installer in tests.
    for artifact in artifacts:
        artifact.unlink()
    (middleware / "migrate-native.exe").unlink()
    incomplete = subprocess.run([str(compiler), "/DMyArch=x64", "/DMyAppVersion=0.0.0", "/O" + str(output),
                                 str(script_dir / "installer.iss")], capture_output=True, text=True, errors="replace")
    assert incomplete.returncode != 0, "An installer must not compile without its automatic upgrade executable"
    assert "migrate-native.exe" in incomplete.stdout + incomplete.stderr
