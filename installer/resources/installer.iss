; =================================================================================
; SCRIPT USAGE AND COMPILATION GUIDE (REVISED)
;
; This guide explains how to compile this script to generate 32-bit and 64-bit
; installers, and how to run the final executable with different parameters.
;
; =================================================================================
;
; --- I. How to Compile 32-bit and 64-bit Installers ---
;
; This script is configured to produce separate installers for 32-bit (x86) and
; 64-bit (x64) architectures. The choice is controlled by the 'MyArch' preprocessor
; variable, which defaults to "x86" within this script.
;
; 1. TO COMPILE THE 32-BIT (x86) INSTALLER (Default):
;    -------------------------------------------------
;    Simply compile the script without any special flags. The internal definition
;    '#define MyArch "x86"' will be used.
;
;    Using the Command-Line Compiler (ISCC.exe):
;    > iscc.exe "YourScriptFileName.iss"
;
;    This will produce the 32-bit output file 'velide_install_x86.exe'.
;
; 2. TO COMPILE THE 64-BIT (x64) INSTALLER (Override):
;    --------------------------------------------------
;    You must override the default value of 'MyArch' by passing a definition to the
;    command-line compiler. This is the recommended method for automation.
;
;    Using the Command-Line Compiler (ISCC.exe):
;    > iscc.exe /DMyArch=x64 "YourScriptFileName.iss"
;
;    The `/DMyArch=x64` switch takes precedence over the in-script '#define',
;    instructing the compiler to build the 64-bit version. This will produce
;    the 64-bit output file 'velide_install_x64.exe'.
;
;    (Manual IDE Method: If you are not using the command line, you can temporarily
;    edit the line in the script from '#define MyArch "x86"' to '#define MyArch "x64"'
;    before hitting "Compile".)
;
; =================================================================================
;
; --- II. How to Run the Compiled Installer Executable ---
;
; The generated executable (e.g., 'velide_install_x64.exe') can be run in several ways.
;
; 1. STANDARD INSTALLATION (INTERACTIVE):
;    ------------------------------------
;    Simply double-click the executable or run it from the command line with no parameters.
;    > velide_install_x64.exe
;
;    This will launch the graphical wizard, allowing the user to make choices on the
;    custom pages (System Selection, CDS Configuration, etc.).
;
; 2. SILENT INSTALLATION:
;    ---------------------
;    You can run the installer without a user interface using standard Inno Setup flags.
;    This is useful for scripted deployments.
;    > velide_install_x64.exe /SILENT /VERYSILENT
;
;    - /SILENT shows installation progress but no wizard pages.
;    - /VERYSILENT shows nothing at all.
;    NOTE: A silent install will not run the logic to create a 'config.yml' file, as it
;    skips the custom pages where the user makes their selections.
;
; 3. UPGRADE MODE (FOR APPLICATION AUTO-UPDATER):
;    --------------------------------------------
;    This is a special mode triggered by the custom '/UPDATE=1' command-line parameter.
;    > velide_install_x64.exe /UPDATE=1
;
;    This mode is specifically designed to be called BY YOUR APPLICATION when it needs
;    to perform a self-update. It behaves differently from a standard installation:
;
;    - IT SKIPS ALL CUSTOM WIZARD PAGES: The user will not be asked to select their
;      system or configure folder paths again.
;    - IT PRESERVES EXISTING CONFIGURATION: Because the custom pages are skipped, the
;      installer will NOT overwrite an existing 'config.yml' file. This protects the
;      user's current settings.
;    - IT USES A TEMPORARY DIRECTORY for some installer-related files to ensure a
;      smooth update process.
;
; =================================================================================

#ifndef MyArch
  #define MyArch "x86"
#endif

#define MyAppName "Velide Middleware"
#define MyAppPublisher "Velide"
#define MyAppURL "https://velide.com.br/"
#define MyAppExeName "main.exe"

#ifndef MyAppVersion
  #define MyAppVersion "v3.0.0"
#endif

[Setup]
; NOTE: The value of AppId uniquely identifies this application. Do not use the same AppId value in installers for other applications.
; (To generate a new GUID, click Tools | Generate GUID inside the IDE.)
AppId={{E1B380E2-6B2C-4153-9C3B-EEA96A13B92E}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
DefaultDirName={autopf}\{#MyAppName}
UninstallDisplayIcon={app}\{#MyAppExeName}
; "ArchitecturesAllowed=x64compatible" specifies that Setup cannot run
; on anything but x64 and Windows 11 on Arm.
; ArchitecturesAllowed=x64compatible
; "ArchitecturesInstallIn64BitMode=x64compatible" requests that the
; install be done in "64-bit mode" on x64 or Windows 11 on Arm,
; meaning it should use the native 64-bit Program Files directory and
; the 64-bit view of the registry.
; ArchitecturesInstallIn64BitMode=x64compatible
DisableProgramGroupPage=yes
; Remove the following line to run in administrative install mode (install for all users).
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=commandline
OutputDir=..\..\
SetupIconFile=..\resources\velide.ico
SolidCompression=yes
WizardStyle=modern

#if MyArch == "x64"
  ; 64-bit specific settings
  ArchitecturesInstallIn64BitMode=x64compatible
  ArchitecturesAllowed=x64compatible
  OutputBaseFilename=velide_install_x64
#else
  ; 32-bit specific settings (default)
  ArchitecturesAllowed=x86compatible
  OutputBaseFilename=velide_install_x86
#endif

[Languages]
Name: "en"; MessagesFile: "compiler:Default.isl"
Name: "br"; MessagesFile: "compiler:Languages\BrazilianPortuguese.isl"
Name: "es"; MessagesFile: "compiler:Languages\Spanish.isl"

[CustomMessages]
en.IntegrationConfigTitle=Integration Configuration
br.IntegrationConfigTitle=Configuração da Integração
es.IntegrationConfigTitle=Configuración de Integración

en.IntegrationConfigDescription=Please, select your system.
br.IntegrationConfigDescription=Por favor, selecione seu sistema.
es.IntegrationConfigDescription=Por favor, seleccione su sistema.

en.IntegrationConfigCaption=Select the system you use and which you want to connect to Velide:
br.IntegrationConfigCaption=Selecione o sistema que você utiliza e que deseja integrar com o Velide:
es.IntegrationConfigCaption=Selecciona el sistema que utilizas y al que quieres conectarte con Velide:

; -- Dropdown Items --
en.SystemCDS=CDS
br.SystemCDS=CDS
es.SystemCDS=CDS

en.SystemFarmax=Farmax
br.SystemFarmax=Farmax
es.SystemFarmax=Farmax

en.CDSConfigTitle=CDS Configuration
br.CDSConfigTitle=Configuração do CDS
es.CDSConfigTitle=Configuración de CDS

en.CDSConfigDescription=Please specify the folder for CDS integration.
br.CDSConfigDescription=Por favor, especifique a pasta para integração com o CDS.
es.CDSConfigDescription=Por favor, especifique la carpeta para la integración con CDS.

en.CDSConfigCaption=Select the folder where CDS generates its files to be monitored:
br.CDSConfigCaption=Selecione a pasta onde o CDS gera seus arquivos a serem monitorados:
es.CDSConfigCaption=Seleccione la carpeta donde CDS genera sus archivos para ser monitoreados:

en.CDSObservationCaption=If you are unsure, please contact the CDS support.
br.CDSObservationCaption=Se você não souber, por favor contate o suporte do CDS.
es.CDSObservationCaption=Si no está seguro, comuníquese con el soporte de CDS.

en.SelectFolder=Select folder:
br.SelectFolder=Selecione uma pasta:
es.SelectFolder=Seleccione una carpeta:

en.ErrorFolderRequired=You must select a folder.
br.ErrorFolderRequired=Você deve selecionar uma pasta.
es.ErrorFolderRequired=Debe seleccionar una carpeta.

en.ErrorFolderNotFound=The selected folder does not exist.
br.ErrorFolderNotFound=A pasta selecionada não existe.
es.ErrorFolderNotFound=La carpeta seleccionada no existe.

en.BrowseCaption=Browse...
br.BrowseCaption=Procurar...
es.BrowseCaption=Explorar...

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "startup"; Description: "{cm:AutoStartProgram,{#MyAppName}}"; GroupDescription: "{cm:AutoStartProgramGroupDescription}"; Flags: unchecked

[Files]
; Copy all files AND subdirectories from 'middleware' to the installation folder.
Source: "..\..\middleware\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

; Copy all files AND subdirectories from 'installer' to the 'installer' subfolder.
Source: "..\*"; DestDir: "{code:GetInstallerDestDir}"; Flags: ignoreversion recursesubdirs createallsubdirs
; Source: "middleware\resources\config_example.yml"; DestDir: "{app}\installer"; DestName: "config.yml"; Flags: ignoreversion onlyifdoesntexist
; NOTE: Don't use "Flags: ignoreversion" on any shared system files

[Code]
var
  // --- Page 1: System Selection ---
  SystemSelectionPage: TWizardPage;
  DropDownLabel: TNewStaticText;
  SystemDropDown: TNewComboBox;
  // --- Page 2: CDS Configuration (Conditional) ---
  CDSConfigPage: TWizardPage;
  CDSFolderInput: TEdit;  
  CDSBrowseButton: TButton;
  CDSLabel: TNewStaticText;
  CDSFolderNote: TNewStaticText;

  // --- Variable to store the selected folder path ---
  SelectedFolderPath: String;
  
  IsUpgrade: Boolean;
  TempInstallerDir: String;
  
//
// NEW FUNCTION: Determines where the installer folder should be placed
//
function GetInstallerDestDir(Param: String): String;
begin
  if IsUpgrade then
  begin
    // During upgrade, install to temporary directory
    Result := TempInstallerDir;
    // Log(Format('Using temporary installer directory: %s', [Result]));
  end
  else
  begin
    // During initial install, use normal installer subdirectory
    Result := ExpandConstant('{app}\installer');
    Log(Format('Using normal installer directory: %s', [Result]));
  end;
end;
  
function _IsUpgrade(): Boolean;
var
  I: Integer;
begin
  for I := 1 to ParamCount do
  begin
    Log(Format('params croped %s', [Copy(ParamStr(I), 1, 7)]));
    Log(Format('parames %s', [ParamStr(I)]));
    Log(Format('remaining %s', [Copy(ParamStr(I), 9, Length(ParamStr(I)))]));
    if CompareText(Copy(ParamStr(I), 1, 7), '/UPDATE') = 0 then
    begin
      // Parameter found, extract the value after the '='
      Result := CompareText(Copy(ParamStr(I), 9, Length(ParamStr(I))), '1') = 0;
      Exit;
    end;
  end;
  Log('Ta nn');
  Result := False
end;

//
// This is a custom helper function to perform a Unicode-safe string replacement.
// It mimics the behavior of the standard Delphi StringReplace function.
//
function StringReplace(const S, OldPattern, NewPattern: String): String;
var
  Position: Integer;
begin
  Result := S;
  Position := Pos(OldPattern, Result);
  while Position > 0 do
  begin
    Delete(Result, Position, Length(OldPattern));
    Insert(NewPattern, Result, Position);
    Position := Pos(OldPattern, Result);
  end;
end;

procedure BrowseButtonClick(Sender: TObject);
var
  SelectedFolder: String;
begin
  SelectedFolder := CDSFolderInput.Text;
  if BrowseForFolder(CustomMessage('SelectFolder'), SelectedFolder, False) then
  begin
    CDSFolderInput.Text := SelectedFolder;
  end;
end;

function InitializeSetup(): Boolean;
var
  TempPath: String;
begin
  IsUpgrade := _IsUpgrade();
  
  // If this is an upgrade, set up the temporary installer directory
  if IsUpgrade then
  begin
    // Use Windows TEMP directory with a predictable subdirectory name
    TempPath := GetEnv('TEMP');
    if TempPath = '' then
      TempPath := GetEnv('TMP');
    if TempPath = '' then
      TempPath := ExpandConstant('{tmp}'); // Fallback to Inno Setup temp
    
    TempInstallerDir := TempPath + '\velide_installer_update';
    Log(Format('Upgrade detected. Temporary installer directory: %s', [TempInstallerDir]));
    
    // Create the temporary directory if it doesn't exist
    if not DirExists(TempInstallerDir) then
    begin
      if not CreateDir(TempInstallerDir) then
      begin
        Log(Format('Failed to create temporary directory: %s', [TempInstallerDir]));
        MsgBox('Failed to create temporary update directory. Installation cannot continue.', mbError, MB_OK);
        Result := False;
        Exit;
      end;
    end;
  end;
  
  Result := True;
end;

//
// This procedure is called when the installer wizard is first created.
// We use it to build our custom page and its components from scratch.
//
procedure InitializeWizard();
begin
  // 1. Create the custom wizard page itself.
  SystemSelectionPage := CreateCustomPage(wpSelectTasks, CustomMessage('IntegrationConfigTitle'), CustomMessage('IntegrationConfigDescription'));
  
  // 2. Create a text label on our new page.
  DropDownLabel := TNewStaticText.Create(SystemSelectionPage);
  DropDownLabel.Caption := CustomMessage('IntegrationConfigCaption');
  DropDownLabel.Parent := SystemSelectionPage.Surface; // Attach it to the page
  DropDownLabel.SetBounds(ScaleX(10), ScaleY(10), ScaleX(400), ScaleY(20)); // Position and size

  // 3. Create the dropdown menu (TNewComboBox) component.
  SystemDropDown := TNewComboBox.Create(SystemSelectionPage);
  SystemDropDown.Parent := SystemSelectionPage.Surface; // Attach it to the page
  SystemDropDown.SetBounds(ScaleX(10), ScaleY(35), ScaleX(250), ScaleY(21)); // Position it below the label
  SystemDropDown.Style := csDropDownList; // Makes it a non-editable dropdown

  // 4. Add the string options to the dropdown list.
  SystemDropDown.Items.Add(CustomMessage('SystemCDS'));
  SystemDropDown.Items.Add(CustomMessage('SystemFarmax'));
  
  // 5. Set a default selection (the first item, index 0).
  SystemDropDown.ItemIndex := 0;
   
  // --- Create the second custom page (CDS Configuration) ---
  // It is created after the first custom page. Its visibility will be controlled later.
  CDSConfigPage := CreateCustomPage(SystemSelectionPage.ID, CustomMessage('CDSConfigTitle'), CustomMessage('CDSConfigDescription'));

  CDSLabel := TNewStaticText.Create(CDSConfigPage);
  CDSLabel.Caption := CustomMessage('CDSConfigCaption');
  CDSLabel.Parent := CDSConfigPage.Surface;// Attach it to the page
  CDSLabel.SetBounds(ScaleX(10), ScaleY(10), ScaleX(400), ScaleY(20)); // Position and size
  
  // Create the folder input field
  CDSFolderInput := TEdit.Create(CDSConfigPage);
  CDSFolderInput.Parent := CDSConfigPage.Surface;
  CDSFolderInput.SetBounds(ScaleX(10), ScaleY(35), ScaleX(320), ScaleY(21));

  // Create the browse button
  CDSBrowseButton := TButton.Create(CDSConfigPage);
  CDSBrowseButton.Parent := CDSConfigPage.Surface;
  CDSBrowseButton.SetBounds(ScaleX(340), ScaleY(35), ScaleX(70), ScaleY(21));
  CDSBrowseButton.Caption := CustomMessage('BrowseCaption');
  CDSBrowseButton.OnClick := @BrowseButtonClick;
  
  // Create the observation/note below the input
  CDSFolderNote := TNewStaticText.Create(CDSConfigPage);
  CDSFolderNote.Caption := CustomMessage('CDSObservationCaption');
  CDSFolderNote.Parent := CDSConfigPage.Surface;
  CDSFolderNote.SetBounds(ScaleX(10), ScaleY(62), ScaleX(400), ScaleY(30));
  CDSFolderNote.Font.Style := [fsItalic];  // Optional: make it italic
  CDSFolderNote.Font.Color := clGray;      // Optional: make it gray
end;

//
// This event function is called when the wizard is about to show a page.
// We use it to conditionally skip the CDS configuration page.
//
function ShouldSkipPage(PageID: Integer): Boolean;
begin
  Result := False; // Default: do not skip the page
  // Skip custom pages if it's an upgrade
  if IsUpgrade then
  begin
    if (PageID = SystemSelectionPage.ID) or (PageID = CDSConfigPage.ID) then
    begin
      Result := True;
      Exit;
    end;
  end;
  
  if PageID = CDSConfigPage.ID then
  begin
    // If the selected system is NOT 'CDS', then we skip this page.
    if SystemDropDown.Text <> CustomMessage('SystemCDS') then
    begin
      Result := True;
    end;
  end;
end;

//
// This event function is called when the user clicks the Next button.
// We use it to validate the input on our custom pages.
//
function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True; // Default: allow proceeding
  IsUpgrade := _IsUpgrade();
  if IsUpgrade then
  begin
    Result := True;
    Exit;
  end;
  
  if CurPageID = CDSConfigPage.ID then
  begin
    Result := True; // Default: allow proceeding
  
    // Validate the folder input on the CDS configuration page.
    if Trim(CDSFolderInput.Text) = '' then
    begin
      MsgBox(CustomMessage('ErrorFolderRequired'), mbError, MB_OK);
      Result := False; // Prevent moving to the next page
    end
    else if not DirExists(CDSFolderInput.Text) then
    begin
      MsgBox(CustomMessage('ErrorFolderNotFound'), mbError, MB_OK);
      Result := False; // Prevent moving to the next page
    end
    else
    begin
      // If validation is successful, store the path.
      SelectedFolderPath := CDSFolderInput.Text;
    end;
  end;
end;
//
// This procedure is called when the installer switches to a new step.
// We use it to process the config file after all files are installed.
//
procedure CurStepChanged(CurStep: TSetupStep);
var
  TemplatePath, ConfigPath, SelectedSystem: String;
  FileContent: TArrayOfString;
  i: Integer;
begin
  if CurStep = ssPostInstall then
  begin
    TemplatePath := ExpandConstant('{app}\resources\config_example.yml');
    ConfigPath := ExpandConstant('{app}\resources\config.yml');

    if not FileExists(ConfigPath) then
    begin
      Log('config.yml not found. Creating from template...');
      
      SelectedSystem := SystemDropDown.Text;
      
      if LoadStringsFromFile(TemplatePath, FileContent) then
      begin
        // Iterate through each line of the file to perform replacements
        for i := 0 to GetArrayLength(FileContent) - 1 do
        begin
          // Always replace the target system
          FileContent[i] := StringReplace(FileContent[i], '{{ TARGET_SYSTEM }}', SelectedSystem);
          
          // --- NEW: Conditionally replace the folder path ---
          // Only perform this replacement if the selected system was CDS
          if SelectedSystem = CustomMessage('SystemCDS') then
          begin
            FileContent[i] := StringReplace(FileContent[i], '{{ FOLDER_TO_WATCH }}', SelectedFolderPath);
          end;
        end;
        
        // Save the modified content to the new config.yml file as UTF-8
        if SaveStringsToUTF8File(ConfigPath, FileContent, True) then
        begin
          Log(Format('Successfully created config.yml with system: %s', [SelectedSystem]));
          if SelectedSystem = CustomMessage('SystemCDS') then
          begin
            Log(Format('CDS folder to watch set to: %s', [SelectedFolderPath]));
          end;
        end
        else
        begin
          Log(Format('Error saving file %s', [ConfigPath]));
        end;
      end
      else
      begin
        Log(Format('Error reading template file %s', [TemplatePath]));
      end;
    end
    else
    begin
      Log('config.yml already exists. Skipping creation to preserve user settings.');
    end;
  end;
end;

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Parameters: "--is-update-checked"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

