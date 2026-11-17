"""First-hop proof using the original batch layout and real Windows copy/path commands."""
import os
from pathlib import Path
import re
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[2]
RESOURCES = ROOT / "installer" / "resources"
# Original executable statements from base 382e70b2352bdfcd65159c174a27ac03f7fa98b9.
ORIGINAL = ROOT / "tests" / "fixtures" / "upgrade" / "original_apply_update.bat"


def resolved_restart(script, cwd):
    content = script.read_text(encoding="utf-8")
    executable = next(line for line in content.splitlines() if line.startswith('set "MAIN_EXECUTABLE='))
    start = content.index('pushd "%~dp0"')
    finish = content.index("\npopd", start) + len("\npopd")
    probe = script.with_name("restart-path-probe.cmd")
    # Execute the actual path-resolution excerpt in the SAME installed resource
    # directory. Deliberately omit START: no application/installer is executed.
    probe.write_text("@echo off\nsetlocal\n" + executable + "\n" + content[start:finish] +
                     "\necho UPGRADE_RESOLVED=%ResolvedAppPath%\n", encoding="ascii")
    result = subprocess.run([os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", str(probe)],
                            cwd=str(cwd), capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    marker = next(line for line in result.stdout.splitlines() if line.startswith("UPGRADE_RESOLVED="))
    probe.unlink()
    return Path(marker.split("=", 1)[1]).resolve()


@pytest.mark.skipif(os.name != "nt", reason="Exercises Windows CMD and robocopy used by the installed updater")
@pytest.mark.parametrize("upgrade_mode", [True, False])
def test_original_installed_batch_is_replaced_directly_before_old_updater_launches_it(tmp_path, upgrade_mode):
    application = tmp_path / "Old Middleware with spaces"
    updater = application / "installer"
    installed_batch = updater / "resources" / "apply_update.bat"
    installed_batch.parent.mkdir(parents=True)
    installed_batch.write_bytes(ORIGINAL.read_bytes())
    (application / "main.exe").write_bytes(b"test application placeholder, never executed")
    (updater / "main.exe").write_bytes(b"old updater placeholder")
    (application / "resources").mkdir()
    config = application / "resources" / "config.yml"
    config.write_bytes(b"preserved installation configuration")
    assert '/XF "%SELF_SCRIPT_NAME%"' in installed_batch.read_text()
    assert resolved_restart(installed_batch, tmp_path) != application / "main.exe"

    staging = tmp_path / "velide_installer_update" if upgrade_mode else updater
    text = (RESOURCES / "files_and_run.iss").read_text(encoding="utf-8")
    entries = re.findall(r'^Source: "apply_update\.bat"; DestDir: "([^\"]+)"; Flags: ([^\r\n]+)', text, re.MULTILINE)
    assert ("{app}\\installer\\resources", "ignoreversion") in entries
    assert ("{code:GetInstallerDestDir}\\resources", "ignoreversion") in entries
    # Project the actual installer entries onto the original installed layout.
    # Setup finishes these file replacements before returning to the old updater.
    for destination, flags in entries:
        assert "onlyifdoesntexist" not in flags
        directory = Path(destination.replace("{app}", str(application)).replace("{code:GetInstallerDestDir}", str(staging)))
        directory.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(str(RESOURCES / "apply_update.bat"), str(directory / "apply_update.bat"))
    corrected = (RESOURCES / "apply_update.bat").read_bytes()
    assert installed_batch.read_bytes() == corrected  # Required BEFORE robocopy.
    assert resolved_restart(installed_batch, tmp_path) == application / "main.exe"

    if upgrade_mode:
        (staging / "main.exe").write_bytes(b"new updater placeholder")
        (staging / "lib").mkdir()
        (staging / "lib" / "replacement.txt").write_bytes(b"new runtime fixture")
        # The self-excluded staged copy cannot undo the direct replacement.
        (staging / "resources" / "apply_update.bat").write_bytes(b"staged self file excluded by robocopy")
        result = subprocess.run(["robocopy", str(staging), str(updater), "/E", "/MOVE", "/IS",
                                 "/XF", "apply_update.bat", "/R:0", "/W:0"], capture_output=True)
        assert result.returncode < 8, result.stdout + result.stderr
        assert (updater / "main.exe").read_bytes() == b"new updater placeholder"
        assert (updater / "lib" / "replacement.txt").read_bytes() == b"new runtime fixture"
        assert installed_batch.read_bytes() == corrected
        assert resolved_restart(installed_batch, tmp_path) == application / "main.exe"
    assert config.read_bytes() == b"preserved installation configuration"
