"""Release inputs and customer-state boundaries; no network or live installation."""
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from uuid import uuid4

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]
CLI = ROOT / "installer" / "src" / "cli" / "release_resources.py"
INSTALLER = ROOT / "installer" / "resources"
SPEC = importlib.util.spec_from_file_location("rollout_release_resources", CLI)
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)
# Synthetic syntax fixture only: tests never resolve or contact this hostname.
BASE_URL = "https://middleware.release-fixture.org/api/middleware/v1"


@pytest.mark.parametrize("value", [
    None, "", " ", BASE_URL + "/", BASE_URL + "?x=1", BASE_URL + "?",
    BASE_URL + "#fragment", BASE_URL + "#", BASE_URL + "\n",
    BASE_URL.replace("https://", "http://"),
    BASE_URL.replace("/api/middleware/v1", ""),
    BASE_URL.replace("/api/middleware/v1", "/graphql"),
    BASE_URL.replace("/v1", "/v2"),
    BASE_URL.replace("/v1", "/v1/../v1"),
    BASE_URL.replace("https://", "https://user:secret@"),
    BASE_URL.replace("https://", "https://@"),
    BASE_URL.replace("https://", "https://\\"),
    BASE_URL.replace(".org/", ".org:0/"),
    BASE_URL.replace(".org/", ".org:65536/"),
    BASE_URL.replace(".org/", ".org:invalid/"),
    BASE_URL.replace(".org/", ".org:/"),
    "https://[::1]/api/middleware/v1",
    "https://[broken/api/middleware/v1",
] + [
    "https://" + host + "/api/middleware/v1"
    for host in (
        "localhost", "127.0.0.1", "10.1.2.3", "169.254.169.254", "8.8.8.8",
        "2130706433", "middleware.localhost", "middleware.local",
        "middleware.internal", "deployment-required.invalid",
        "middleware.test", "middleware.example", "example.com",
        "middleware.example.org", "example.net", "missing-host",
        "-bad.release.org", "bad_.release.org", "bad..org", "host.org.",
    )
])
def test_release_rejects_missing_unsafe_or_placeholder_url(value):
    with pytest.raises(ValueError, match="MIDDLEWARE_SERVICE_BASE_URL"):
        release.deployment_base_url(value)


@pytest.mark.parametrize("value", [BASE_URL, BASE_URL.replace(".org/", ".org:8443/")])
def test_release_accepts_complete_explicit_https_endpoint(value):
    assert release.deployment_base_url(value) == value


def test_generation_requires_setting_even_with_previous_output(tmp_path, monkeypatch):
    destination = tmp_path / release.SERVICE_RESOURCE
    monkeypatch.setenv(release.SERVICE_URL_ENV, BASE_URL)
    release.generate_service_resource(destination)
    before = destination.read_bytes()
    assert json.loads(before) == {"version": 1, "base_url": BASE_URL}
    assert not list(tmp_path.glob("*.pending"))
    for invalid in (None, "", "http://localhost/api/middleware/v1"):
        if invalid is None:
            monkeypatch.delenv(release.SERVICE_URL_ENV)
        else:
            monkeypatch.setenv(release.SERVICE_URL_ENV, invalid)
        with pytest.raises(ValueError):
            release.generate_service_resource(destination)
        assert destination.read_bytes() == before


def test_cli_fails_closed_and_freeze_uses_generated_resource(tmp_path, monkeypatch):
    monkeypatch.delenv(release.SERVICE_URL_ENV, raising=False)
    destination = tmp_path / release.SERVICE_RESOURCE
    failed = subprocess.run(
        [sys.executable, str(CLI), "generate", str(destination)],
        capture_output=True, text=True,
    )
    assert failed.returncode == 2
    assert release.SERVICE_URL_ENV in failed.stderr
    assert not destination.exists()
    monkeypatch.setenv(release.SERVICE_URL_ENV, BASE_URL)
    generated = subprocess.run(
        [sys.executable, str(CLI), "generate", str(destination)],
        capture_output=True, text=True,
    )
    assert generated.returncode == 0, generated.stderr
    files = dict(release.resource_files(
        ROOT / "resources", service_resource=destination,
    ))
    assert files[str(destination)] == "resources/" + release.SERVICE_RESOURCE
    assert str(ROOT / "resources" / release.SERVICE_RESOURCE) not in files
    placeholder = json.loads(
        (ROOT / "resources" / release.SERVICE_RESOURCE).read_text()
    )
    assert placeholder["version"] == 1
    with pytest.raises(ValueError):
        release.deployment_base_url(placeholder["base_url"])


@pytest.mark.parametrize("source", ["CDS", "Farmax"])
def test_fresh_template_has_explicit_marker_and_no_legacy_defaults(source):
    from config import Settings

    template = (ROOT / "resources" / "config_example.yml").read_text()
    rendered = template.replace("{{ TARGET_SYSTEM }}", source)
    if source == "CDS":
        rendered = rendered.replace("{{ FOLDER_TO_WATCH }}", r"D:\\ERP\\CDS")
    else:
        rendered = rendered.replace('"{{ FOLDER_TO_WATCH }}"', "null")
        rendered += '\nfarmax:\n  host: localhost\n  file: "D:\\\\ERP\\\\store.fdb"\n'
        rendered += "  user: fixture-user\n  password: fixture-password\n"
    document = yaml.safe_load(rendered)
    assert document["onboarding"] == "native_required"
    assert not {"auth", "api", "backend_profiles", "active_backend"} & document.keys()
    settings = Settings(**document)
    assert settings.onboarding == "native_required"
    assert settings.auth is None and settings.api is None
    assert "https://" not in template and "wss://" not in template
    if source == "Farmax":
        assert settings.folder_to_watch is None


def make_frozen(root, monkeypatch, updater=False):
    assets = release.UPDATER_RESOURCES if updater else release.MAIN_RESOURCES
    for name in assets:
        path = root / "resources" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        source = (INSTALLER if updater else ROOT / "resources") / name
        path.write_bytes(source.read_bytes())
    (root / "main.exe").write_bytes(b"compile fixture")
    (root / "lib").mkdir()
    (root / "lib" / "library.zip").write_bytes(b"compile fixture")
    if not updater:
        (root / "migrate-native.exe").write_bytes(b"compile fixture")
        monkeypatch.setenv(release.SERVICE_URL_ENV, BASE_URL)
        release.generate_service_resource(root / "resources" / release.SERVICE_RESOURCE)
    return root


@pytest.mark.parametrize("updater", [False, True])
@pytest.mark.parametrize("extra", [
    "resources/config.yml.rollout.json",
    "resources/config.yml.automatic-native-upgrade.json",
    "resources/config.yml.prepared-native-token.txt",
    "resources/native-upgrade.json.pending",
    "resources/native.db-wal",
    "resources/receipts/applied.json",
    "rollout.json", "native-upgrade.json", "native.db.token.txt",
    "customer-packages/opaque-package.json",
])
def test_frozen_check_rejects_customer_state(tmp_path, monkeypatch, updater, extra):
    make_frozen(tmp_path, monkeypatch, updater=updater)
    release.check_frozen_resources(tmp_path, updater=updater)
    path = tmp_path / extra
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"private installation state")
    with pytest.raises(ValueError):
        release.check_frozen_resources(tmp_path, updater=updater)


def test_frozen_check_rejects_placeholder_mismatch_or_missing_helper(
    tmp_path, monkeypatch,
):
    make_frozen(tmp_path, monkeypatch)
    resource = tmp_path / "resources" / release.SERVICE_RESOURCE
    good = resource.read_bytes()
    resource.write_bytes((ROOT / "resources" / release.SERVICE_RESOURCE).read_bytes())
    with pytest.raises(ValueError):
        release.check_frozen_resources(tmp_path)
    resource.write_bytes(good)
    monkeypatch.setenv(
        release.SERVICE_URL_ENV, BASE_URL.replace("middleware.", "other.")
    )
    with pytest.raises(ValueError):
        release.check_frozen_resources(tmp_path)
    monkeypatch.setenv(release.SERVICE_URL_ENV, BASE_URL)
    (tmp_path / "migrate-native.exe").unlink()
    with pytest.raises(FileNotFoundError):
        release.check_frozen_resources(tmp_path)


def test_installer_captures_freshness_before_copy_and_guards_manual_repairs():
    logic = (INSTALLER / "logic.pas").read_text()
    prepare = logic.split("function PrepareToInstall", 1)[1]
    assert (
        "IsFreshInstall := (not IsUpgrade) and (not HasInstallationEvidence())"
        in prepare
    )
    assert "IsUpgrade and not InstalledConfigExists()" in prepare
    assert "(not IsFreshInstall) and not InstalledConfigExists()" in prepare
    evidence = logic.split("function HasInstallationEvidence", 1)[1].split("end;", 1)[0]
    for marker in ("InstalledConfigExists()", r"{app}\main.exe",
                   r"{app}\resources", r"{app}\installer", r"{app}\unins000.exe"):
        assert marker in evidence
    pages = logic.split("function ShouldSkipPage", 1)[1].split("end;", 1)[0]
    assert "IsUpgrade or HasInstallationEvidence()" in pages
    assert pages.index("PageID = SystemSelectionPage.ID") < pages.index("HasInstallationEvidence()")
    post = logic.split("procedure CurStepChanged", 1)[1]
    assert post.index("(not IsFreshInstall)") < post.index("LoadStringsFromFile")
    assert "if not FileExists(ConfigPath) then" in post
    assert "SaveStringsToUTF8File(ConfigPath, FileContent, False)" in post
    assert "Result := ConfigNeedsAutomaticUpgrade(" in post
    assert r"ExpandConstant('{app}\resources\config.yml'), IsFreshInstall)" in post
    files = (INSTALLER / "files_and_run.iss").read_text()
    assert "Check: ShouldUpgradeInstalledConfig" in files
    assert "[InstallDelete]" not in files and "[UninstallDelete]" not in files
    assert 'Source: "..\\..\\middleware\\*";' not in files
    assert 'Source: "..\\*";' not in files


def test_workflow_requires_release_variable_and_checks_before_compile():
    workflow = (ROOT / ".github" / "workflows" / "create-release.yml").read_text()
    assert (
        "MIDDLEWARE_SERVICE_BASE_URL: ${{ vars.MIDDLEWARE_SERVICE_BASE_URL }}"
        in workflow
    )
    assert workflow.index("release_resources.py generate") < workflow.index(
        "setup.py build"
    )
    assert (
        workflow.index("release_resources.py check output/middleware")
        < workflow.index("Minionguyjpro/Inno-Setup-Action")
    )
    assert "'./output/release-resources'" in workflow
    setup = (CLI.parent / "setup.py").read_text()
    assert "generate_service_resource(" in setup
    assert "service_resource=SERVICE_RESOURCE" in setup


@pytest.fixture(scope="module")
def inno_upgrade_gate_probe(tmp_path_factory):
    """Execute the production Pascal predicate; abort before any installation."""
    compiler = Path(os.environ.get(
        "VELIDE_ISCC", r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
    ))
    if not compiler.is_file():
        pytest.skip("Inno Setup compiler is not available")
    directory = tmp_path_factory.mktemp("inno-upgrade-gate")
    logic = (INSTALLER / "logic.pas").read_text()
    predicate = "function ConfigNeedsAutomaticUpgrade" + logic.split(
        "function ConfigNeedsAutomaticUpgrade", 1
    )[1].split("function ShouldUpgradeInstalledConfig", 1)[0]
    script = directory / "probe.iss"
    script.write_text(
        "[Setup]\nAppName=Rollout packaging predicate probe\nAppVersion=0\n"
        "DefaultDirName={tmp}\\unused-rollout-probe\nCreateAppDir=no\n"
        "Uninstallable=no\nPrivilegesRequired=lowest\nOutputBaseFilename=probe\n"
        "[Code]\n" + predicate + """
function InitializeSetup(): Boolean;
var
  Decision: String;
begin
  if ConfigNeedsAutomaticUpgrade(ExpandConstant('{param:CONFIG}'),
      ExpandConstant('{param:FRESH|0}') = '1') then
    Decision := 'run'
  else
    Decision := 'skip';
  if not SaveStringToFile(ExpandConstant('{param:RESULT}'), Decision, False) then
    RaiseException('Cannot write probe result.');
  Result := False;
end;
""",
        encoding="utf-8",
    )
    compiled = subprocess.run(
        [str(compiler), "/O" + str(directory), str(script)],
        capture_output=True, text=True, errors="replace", timeout=120,
    )
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr
    return directory / "probe.exe"


@pytest.mark.parametrize("config_text,fresh,expected", [
    (None, False, "skip"),
    ("auth: {}\napi: {}\n", True, "skip"),
    ("onboarding: native_required\n", False, "skip"),
    ("\ufeffonboarding: native_required\r\n", False, "skip"),
    ('onboarding: "native_required" # retained setup\n', False, "skip"),
    ("'onboarding': 'native_required'\n", False, "skip"),
    ('"onboarding":\t native_required\t# retained setup\n', False, "skip"),
    ("# onboarding: native_required\nauth: {}\napi: {}\n", False, "run"),
    ("extra:\n  onboarding: native_required\nauth: {}\napi: {}\n", False, "run"),
    ("onboarding: native_required_other\nauth: {}\napi: {}\n", False, "run"),
    ("onboarding: null\nauth: {}\napi: {}\n", False, "run"),
    ("auth: {}\napi: {}\n", False, "run"),
    ("auth: {}\napi:\n  native: {}\n", False, "run"),
])
def test_real_inno_upgrade_gate_preserves_pending_setup(
    inno_upgrade_gate_probe, tmp_path, config_text, fresh, expected,
):
    config = tmp_path / "preserved config.yml"
    receipt = tmp_path / "gate-result.txt"
    if config_text is not None:
        config.write_text(config_text, encoding="utf-8")
    original_config = config.read_bytes() if config.exists() else None
    sentinel = tmp_path / "token.txt"
    sentinel.write_bytes(b"installation-owned token fixture")
    process = subprocess.run(
        [str(inno_upgrade_gate_probe), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/SP-",
         "/CONFIG=" + str(config), "/RESULT=" + str(receipt),
         "/FRESH=" + ("1" if fresh else "0")],
        capture_output=True, text=True, errors="replace", timeout=30,
    )
    # InitializeSetup deliberately returns False, before installing any payload.
    assert process.returncode != 0
    assert receipt.read_text() == expected
    assert sentinel.read_bytes() == b"installation-owned token fixture"
    if config_text is not None:
        assert config.read_bytes() == original_config
    else:
        assert not config.exists()


@pytest.mark.parametrize("use_previous_dir", [True, False])
def test_real_interactive_inno_initializes_wizard_before_app_evidence(
    tmp_path, monkeypatch, use_previous_dir,
):
    """Run production InitializeWizard/ShouldSkipPage in Inno's real lifecycle.

    No /SILENT flag: directory-page initialization previously expanded {app}
    before Inno had assigned it. A test-only page event records readiness and
    closes the wizard, without progressing to installation or touching a store.
    """
    compiler = Path(os.environ.get(
        "VELIDE_ISCC", r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
    ))
    if not compiler.is_file():
        pytest.skip("Inno Setup compiler is not available")
    # Preserve actual Setup directives, Tasks and Files checks too. In particular,
    # forcing DisableDirPage=no in a minimal harness misses Inno's early
    # automatic page-skipping query. UsePreviousAppDir=no (as in an isolated
    # rehearsal) triggers that query before {app} is initialized.
    make_frozen(tmp_path / "output" / "middleware", monkeypatch)
    updater = make_frozen(tmp_path / "output" / "installer", monkeypatch, updater=True)
    script = updater / "resources" / "installer.iss"
    settings = script.parent / "settings.iss"
    text = settings.read_text(encoding="utf-8")
    # Isolate from this workstation's previous installs without changing the
    # automatic directory-page behavior. Neither case installs any payload.
    text = re.sub(r"(?m)^AppId=.*$", "AppId=" + str(uuid4()), text)
    if not use_previous_dir:
        text = text.replace("[Setup]", "[Setup]\nUsePreviousAppDir=no")
    settings.write_text(text, encoding="utf-8")
    (script.parent / "vc_redist.x64.exe").write_bytes(b"unused test prerequisite")
    script.write_text(script.read_text(encoding="utf-8") + r"""
procedure CurPageChanged(CurPageID: Integer);
begin
  if not SaveStringToFile(ExpandConstant('{param:RESULT}'),
      'wizard-ready:' + IntToStr(CurPageID), False) then
    RaiseException('Cannot record interactive startup.');
  PostMessage(WizardForm.Handle, $0010, 0, 0);
end;

procedure CancelButtonClick(CurPageID: Integer; var Cancel, Confirm: Boolean);
begin
  Cancel := True;
  Confirm := False;
end;
""", encoding="utf-8")
    compiled = tmp_path / "compiled"
    compiled.mkdir()
    built = subprocess.run(
        [str(compiler), "/DMyArch=x64", "/DMyAppVersion=0.0.0",
         "/O" + str(compiled), str(script)],
        capture_output=True, text=True, errors="replace", timeout=120,
    )
    assert built.returncode == 0, built.stdout + built.stderr
    destination = tmp_path / "fresh-native"
    receipt = tmp_path / "interactive-ready.txt"
    log = tmp_path / "interactive-startup.log"
    environment = dict(os.environ, TEMP=str(tmp_path), TMP=str(tmp_path))
    process = subprocess.Popen(
        [str(compiled / "velide_install_x64.exe"), "/SP-", "/LANG=en",
         "/SUPPRESSMSGBOXES", "/NORESTART", "/DIR=" + str(destination),
         "/RESULT=" + str(receipt), "/LOG=" + str(log)],
        env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    timed_out = False
    stdout = stderr = b""
    try:
        stdout, stderr = process.communicate(timeout=20)
    except subprocess.TimeoutExpired:
        timed_out = True
    finally:
        if process.poll() is None:
            # Inno's bootstrap owns a child process. Reap only this test's tree
            # even if a regression traps it in a pre-wizard error dialog.
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           capture_output=True, timeout=10)
            stdout, stderr = process.communicate(timeout=10)
    diagnostic = log.read_text(encoding="utf-8-sig", errors="replace") if log.exists() else repr(stdout + stderr)
    assert not timed_out, diagnostic
    assert receipt.exists(), diagnostic
    assert receipt.read_text().startswith("wizard-ready:")
    assert process.returncode != 0  # Test-only cancellation, never installation.
    assert "before it was initialized" not in diagnostic
    assert "Runtime error" not in diagnostic
    assert not destination.exists()


def test_real_inno_excludes_rollout_state_at_all_application_boundaries(
    tmp_path, monkeypatch,
):
    compiler = Path(os.environ.get(
        "VELIDE_ISCC", r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
    ))
    if not compiler.is_file():
        pytest.skip("Inno Setup compiler is not available")
    middleware = make_frozen(tmp_path / "output" / "middleware", monkeypatch)
    updater = make_frozen(tmp_path / "output" / "installer", monkeypatch, updater=True)
    script_dir = updater / "resources"
    (script_dir / "vc_redist.x64.exe").write_bytes(b"compile fixture")
    # Runtime module code with rollout in its name must still be packaged.
    (middleware / "lib" / "middleware_rollout.pyc").write_bytes(b"compile fixture")
    forbidden = (
        "rollout-state.json", "native-upgrade.json.pending",
        "config.yml.automatic-native-upgrade.json",
        "config.yml.rollout.json", "config.yml.prepared-native-token.txt",
        "native.db.token.txt", "customer-inventory.json", "native.db-wal",
    )
    for root in (middleware, updater):
        for directory in (root, root / "resources", root / "lib"):
            for name in forbidden:
                (directory / name).write_bytes(b"must never enter public assets")
        (root / "customer-packages").mkdir()
        (root / "customer-packages" / "private-package.json").write_bytes(b"private")
        (root / "resources" / "receipts").mkdir()
        receipt = root / "resources" / "receipts" / "private-receipt.json"
        receipt.write_bytes(b"private")
    compiled = tmp_path / "compiled"
    compiled.mkdir()
    process = subprocess.run(
        [str(compiler), "/DMyArch=x64", "/DMyAppVersion=0.0.0",
         "/O" + str(compiled), str(script_dir / "installer.iss")],
        capture_output=True, text=True, errors="replace", timeout=120,
    )
    log = process.stdout + process.stderr
    assert process.returncode == 0, log
    assert "middleware-service.json" in log
    assert "middleware_rollout.pyc" in log
    for name in forbidden + ("private-package.json", "private-receipt.json"):
        assert name.lower() not in log.lower(), log
    # Ensure every generic resource is in the exact Inno asset list as well.
    entries = re.findall(
        r'Source: "\.\.\\\.\.\\middleware\\resources\\([^"]+)"',
        (INSTALLER / "files_and_run.iss").read_text(),
    )
    assert {name.replace("\\", "/") for name in entries} == set(release.MAIN_RESOURCES)
