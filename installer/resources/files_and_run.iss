; =================================================================================
; FILE: files_and_run.iss
; DESCRIPTION: Defines the files to be installed, shortcuts, and post-install run actions.
;
; DEPENDENCIES:
;   - Preprocessor definitions (from settings.iss): MyArch, MyAppName, MyAppExeName
;   - Pascal Code functions (from logic.pas): GetInstallerDestDir, VCSupportNeeded
; =================================================================================

[Files]
; --- MICROSOFT VISUAL C++ REDISTRIBUTABLE ---
; We bundle the correct runtime based on the architecture defined in settings.iss
#if MyArch == "x64"
  Source: "vc_redist.x64.exe"; DestDir: "{tmp}"; Flags: deleteafterinstall
#else
  Source: "vc_redist.x86.exe"; DestDir: "{tmp}"; Flags: deleteafterinstall
#endif

; --- MAIN APPLICATION FILES ---
; Copy all files AND subdirectories from 'middleware' to the installation folder.
; This includes the 'resources' folder which contains 'config_example.yml'.
; Release artifacts must never overwrite installation-owned state, even if a
; developer accidentally included it in the frozen resources directory.
; Only the root resources tree is replaced by the allowlist below. Keep nested
; runtime resources (Qt, certificates, timezone data and SQLite libraries).
; Explicit executable entries make an incomplete build fail compilation rather
; than silently producing an installer whose automatic-upgrade command is absent.
Source: "..\..\middleware\main.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\..\middleware\migrate-native.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\..\middleware\*"; DestDir: "{app}"; Excludes: "\main.exe,\migrate-native.exe,\resources,\resources\*,\tests,\tests\*,\fixtures,\fixtures\*,*.db*,*.sqlite*,*.lock,\native-upgrade.json,\config.yml,\config.yml.*,\token.txt"; Flags: ignoreversion recursesubdirs createallsubdirs
; Keep synchronized with MAIN_RESOURCES in installer/src/cli/release_resources.py.
Source: "..\..\middleware\resources\config_example.yml"; DestDir: "{app}\resources"; Flags: ignoreversion
Source: "..\..\middleware\resources\error.png"; DestDir: "{app}\resources"; Flags: ignoreversion
Source: "..\..\middleware\resources\loading.png"; DestDir: "{app}\resources"; Flags: ignoreversion
Source: "..\..\middleware\resources\style.css"; DestDir: "{app}\resources"; Flags: ignoreversion
Source: "..\..\middleware\resources\velide.png"; DestDir: "{app}\resources"; Flags: ignoreversion
Source: "..\..\middleware\resources\velide_white.png"; DestDir: "{app}\resources"; Flags: ignoreversion
Source: "..\..\middleware\resources\velide_36.png"; DestDir: "{app}\resources"; Flags: ignoreversion
Source: "..\..\middleware\resources\velide.ico"; DestDir: "{app}\resources"; Flags: ignoreversion
Source: "..\..\middleware\resources\velide_corner.ico"; DestDir: "{app}\resources"; Flags: ignoreversion
Source: "..\..\middleware\resources\fonts\font_regular.ttf"; DestDir: "{app}\resources\fonts"; Flags: ignoreversion
Source: "..\..\middleware\resources\fonts\font_light.ttf"; DestDir: "{app}\resources\fonts"; Flags: ignoreversion
Source: "..\..\middleware\resources\fonts\font_bold.ttf"; DestDir: "{app}\resources\fonts"; Flags: ignoreversion

; --- INSTALLER BACKUP RESOURCES ---
; Copy the installer files themselves to a backup location.
; The destination is dynamic via the Pascal function 'GetInstallerDestDir':
; - Standard Install: {app}\installer
; - Upgrade Mode: A temporary directory (to avoid file locks during self-update)
; First hop: the running OLD updater launches this installed path after Setup
; returns. Its old batch excludes itself from robocopy, so temp staging alone
; cannot replace it. The batch has not started yet and is safe to overwrite now.
Source: "apply_update.bat"; DestDir: "{app}\installer\resources"; Flags: ignoreversion
Source: "..\main.exe"; DestDir: "{code:GetInstallerDestDir}"; Flags: ignoreversion
Source: "..\*"; DestDir: "{code:GetInstallerDestDir}"; Excludes: "\main.exe,\resources,\resources\*,\tests,\tests\*,\fixtures,\fixtures\*,*.db*,*.sqlite*,*.lock,\native-upgrade.json,\config.yml.*,\token.txt"; Flags: ignoreversion recursesubdirs createallsubdirs
; The updater's own config.yml and verification key ARE required runtime assets.
; Keep synchronized with UPDATER_RESOURCES in release_resources.py.
Source: "config.yml"; DestDir: "{code:GetInstallerDestDir}\resources"; Flags: ignoreversion
Source: "private_public.pem"; DestDir: "{code:GetInstallerDestDir}\resources"; Flags: ignoreversion
Source: "apply_update.bat"; DestDir: "{code:GetInstallerDestDir}\resources"; Flags: ignoreversion
Source: "installer.iss"; DestDir: "{code:GetInstallerDestDir}\resources"; Flags: ignoreversion
Source: "files_and_run.iss"; DestDir: "{code:GetInstallerDestDir}\resources"; Flags: ignoreversion
Source: "logic.pas"; DestDir: "{code:GetInstallerDestDir}\resources"; Flags: ignoreversion
Source: "settings.iss"; DestDir: "{code:GetInstallerDestDir}\resources"; Flags: ignoreversion
Source: "error.png"; DestDir: "{code:GetInstallerDestDir}\resources"; Flags: ignoreversion
Source: "loading.png"; DestDir: "{code:GetInstallerDestDir}\resources"; Flags: ignoreversion
Source: "style.css"; DestDir: "{code:GetInstallerDestDir}\resources"; Flags: ignoreversion
Source: "velide.png"; DestDir: "{code:GetInstallerDestDir}\resources"; Flags: ignoreversion
Source: "velide_white.png"; DestDir: "{code:GetInstallerDestDir}\resources"; Flags: ignoreversion
Source: "velide_36.png"; DestDir: "{code:GetInstallerDestDir}\resources"; Flags: ignoreversion
Source: "velide.ico"; DestDir: "{code:GetInstallerDestDir}\resources"; Flags: ignoreversion
Source: "velide_corner.ico"; DestDir: "{code:GetInstallerDestDir}\resources"; Flags: ignoreversion
Source: "fonts\font_regular.ttf"; DestDir: "{code:GetInstallerDestDir}\resources\fonts"; Flags: ignoreversion
Source: "fonts\font_light.ttf"; DestDir: "{code:GetInstallerDestDir}\resources\fonts"; Flags: ignoreversion
Source: "fonts\font_bold.ttf"; DestDir: "{code:GetInstallerDestDir}\resources\fonts"; Flags: ignoreversion

[Icons]
; Start Menu Shortcut
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"

; Desktop Shortcut (Optional, controlled by 'desktopicon' task)
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
; --- RUNTIME PREREQUISITES ---
; Install Visual C++ Redistributable only if the Pascal check (VCSupportNeeded) confirms it is missing.
#if MyArch == "x64"
  Filename: "{tmp}\vc_redist.x64.exe"; \
      Parameters: "/install /passive /norestart"; \
      Check: VCSupportNeeded; \
      Flags: waituntilterminated; \
      StatusMsg: "Installing Microsoft Visual C++ Redistributable (x64)..."
#else
  Filename: "{tmp}\vc_redist.x86.exe"; \
      Parameters: "/install /passive /norestart"; \
      Check: VCSupportNeeded; \
      Flags: waituntilterminated; \
      StatusMsg: "Installing Microsoft Visual C++ Redistributable (x86)..."
#endif

; Runs for silent /UPDATE=1 too. No manifest is needed for an ordinary update.
; The helper keeps legacy endpoints and consumes an optional provisioned package.
; Startup repeats the same idempotent operation before opening any services.
Filename: "{app}\migrate-native.exe"; Parameters: "--automatic --config ""{app}\resources\config.yml"" --bundle-dir ""{app}"""; WorkingDir: "{app}"; Check: InstalledConfigExists; Flags: runhidden waituntilterminated; StatusMsg: "Preserving and upgrading local integration data..."

; --- LAUNCH APPLICATION ---
; Run the main executable after installation finishes.
; The "--is-update-checked" flag is passed to the app to indicate it was just installed/updated.
Filename: "{app}\{#MyAppExeName}"; Parameters: "--is-update-checked"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent
