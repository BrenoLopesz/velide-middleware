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
Source: "..\..\middleware\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

; --- INSTALLER BACKUP RESOURCES ---
; Copy the installer files themselves to a backup location.
; The destination is dynamic via the Pascal function 'GetInstallerDestDir':
; - Standard Install: {app}\installer
; - Upgrade Mode: A temporary directory (to avoid file locks during self-update)
Source: "..\*"; DestDir: "{code:GetInstallerDestDir}"; Flags: ignoreversion recursesubdirs createallsubdirs

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

; --- LAUNCH APPLICATION ---
; Run the main executable after installation finishes.
; The "--is-update-checked" flag is passed to the app to indicate it was just installed/updated.
Filename: "{app}\{#MyAppExeName}"; Parameters: "--is-update-checked"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent