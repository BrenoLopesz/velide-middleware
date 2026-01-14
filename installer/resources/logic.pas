{ =================================================================================
  FILE: logic.pas
  DESCRIPTION: Pascal Scripting logic for the installer.
  
  FUNCTIONS:
    - Custom Wizard Pages (System Selection, CDS Config)
    - Dynamic Configuration File Generation (YAML)
    - Upgrade Logic (/UPDATE=1 flag handling)
    - Prerequisite Checking (VC++ Redistributable)
  ================================================================================= }

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

  // --- Global Variables ---
  SelectedFolderPath: String;
  IsUpgrade: Boolean;
  TempInstallerDir: String;

// ---------------------------------------------------------------------------------
// HELPER: Parse Command Line for Update Flag
// Detects if the installer was launched with /UPDATE=1
// ---------------------------------------------------------------------------------
function _IsUpgrade(): Boolean;
var
  I: Integer;
begin
  Result := False;
  for I := 1 to ParamCount do
  begin
    // Check for parameter starting with /UPDATE
    if CompareText(Copy(ParamStr(I), 1, 7), '/UPDATE') = 0 then
    begin
      // Extract value after '=' (e.g., /UPDATE=1)
      if CompareText(Copy(ParamStr(I), 9, Length(ParamStr(I))), '1') = 0 then
      begin
        Result := True;
        Exit;
      end;
    end;
  end;
end;

// ---------------------------------------------------------------------------------
// FUNCTION: Determines installer destination directory
// Used by [Files] section to decide where to backup the installer.
// ---------------------------------------------------------------------------------
function GetInstallerDestDir(Param: String): String;
begin
  if IsUpgrade then
    Result := TempInstallerDir // Use temp dir during upgrade to avoid file locks
  else
    Result := ExpandConstant('{app}\installer'); // Normal install location
end;

// ---------------------------------------------------------------------------------
// FUNCTION: Custom String Replace
// Helper to perform string replacements in the config template.
// ---------------------------------------------------------------------------------
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

// ---------------------------------------------------------------------------------
// EVENT: Browse Button Click
// Handles folder selection for the CDS configuration page.
// ---------------------------------------------------------------------------------
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

// ---------------------------------------------------------------------------------
// CHECK: VC++ Runtime
// Checks registry to see if the required Visual C++ Redistributable is installed.
// ---------------------------------------------------------------------------------
function VCSupportNeeded: Boolean;
var
  RegKey: string;
  Installed: Cardinal;
begin
  Result := True; // Default: Install it

  #if MyArch == "x64"
    RegKey := 'SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64';
  #else
    RegKey := 'SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x86';
  #endif

  if RegQueryDWordValue(HKLM, RegKey, 'Installed', Installed) then
  begin
    if (Installed = 1) then Result := False;
  end;
end;

// ---------------------------------------------------------------------------------
// INITIALIZE SETUP
// Runs before the wizard starts. Used to detect upgrade mode and prepare temp dirs.
// ---------------------------------------------------------------------------------
function InitializeSetup(): Boolean;
var
  TempPath: String;
begin
  // Calculate IsUpgrade ONCE here
  IsUpgrade := _IsUpgrade();
  
  if IsUpgrade then
  begin
    // Determine a safe temporary directory
    TempPath := GetEnv('TEMP');
    if TempPath = '' then TempPath := GetEnv('TMP');
    if TempPath = '' then TempPath := ExpandConstant('{tmp}');
    
    TempInstallerDir := TempPath + '\velide_installer_update';
    Log(Format('Upgrade detected. Temp dir: %s', [TempInstallerDir]));
    
    // Create the directory if it doesn't exist
    if not DirExists(TempInstallerDir) then
    begin
      if not CreateDir(TempInstallerDir) then
      begin
        MsgBox('Failed to create temporary update directory. Installation cannot continue.', mbError, MB_OK);
        Result := False;
        Exit;
      end;
    end;
  end;
  Result := True;
end;

// ---------------------------------------------------------------------------------
// INITIALIZE WIZARD
// Creates the custom UI pages (System Selection & CDS Config).
// ---------------------------------------------------------------------------------
procedure InitializeWizard();
begin
  // 1. System Selection Page
  SystemSelectionPage := CreateCustomPage(wpSelectTasks, CustomMessage('IntegrationConfigTitle'), CustomMessage('IntegrationConfigDescription'));
  
  DropDownLabel := TNewStaticText.Create(SystemSelectionPage);
  DropDownLabel.Caption := CustomMessage('IntegrationConfigCaption');
  DropDownLabel.Parent := SystemSelectionPage.Surface;
  DropDownLabel.SetBounds(ScaleX(10), ScaleY(10), ScaleX(400), ScaleY(20));

  SystemDropDown := TNewComboBox.Create(SystemSelectionPage);
  SystemDropDown.Parent := SystemSelectionPage.Surface;
  SystemDropDown.SetBounds(ScaleX(10), ScaleY(35), ScaleX(250), ScaleY(21));
  SystemDropDown.Style := csDropDownList;
  SystemDropDown.Items.Add(CustomMessage('SystemCDS'));
  SystemDropDown.Items.Add(CustomMessage('SystemFarmax'));
  SystemDropDown.ItemIndex := 0; // Default to first item
   
  // 2. CDS Configuration Page
  CDSConfigPage := CreateCustomPage(SystemSelectionPage.ID, CustomMessage('CDSConfigTitle'), CustomMessage('CDSConfigDescription'));

  CDSLabel := TNewStaticText.Create(CDSConfigPage);
  CDSLabel.Caption := CustomMessage('CDSConfigCaption');
  CDSLabel.Parent := CDSConfigPage.Surface;
  CDSLabel.SetBounds(ScaleX(10), ScaleY(10), ScaleX(400), ScaleY(20));
  
  CDSFolderInput := TEdit.Create(CDSConfigPage);
  CDSFolderInput.Parent := CDSConfigPage.Surface;
  CDSFolderInput.SetBounds(ScaleX(10), ScaleY(35), ScaleX(320), ScaleY(21));

  CDSBrowseButton := TButton.Create(CDSConfigPage);
  CDSBrowseButton.Parent := CDSConfigPage.Surface;
  CDSBrowseButton.SetBounds(ScaleX(340), ScaleY(35), ScaleX(70), ScaleY(21));
  CDSBrowseButton.Caption := CustomMessage('BrowseCaption');
  CDSBrowseButton.OnClick := @BrowseButtonClick;
  
  CDSFolderNote := TNewStaticText.Create(CDSConfigPage);
  CDSFolderNote.Caption := CustomMessage('CDSObservationCaption');
  CDSFolderNote.Parent := CDSConfigPage.Surface;
  CDSFolderNote.SetBounds(ScaleX(10), ScaleY(62), ScaleX(400), ScaleY(30));
  CDSFolderNote.Font.Style := [fsItalic];
  CDSFolderNote.Font.Color := clGray;
end;

// ---------------------------------------------------------------------------------
// SKIP PAGE LOGIC
// Determines if a page should be skipped (e.g., during upgrade or based on previous choice).
// ---------------------------------------------------------------------------------
function ShouldSkipPage(PageID: Integer): Boolean;
begin
  Result := False;
  
  // If upgrading, skip ALL custom configuration pages
  if IsUpgrade then
  begin
    if (PageID = SystemSelectionPage.ID) or (PageID = CDSConfigPage.ID) then
    begin
      Result := True;
      Exit;
    end;
  end;
  
  // Logic for standard install: Skip CDS page if CDS was not selected
  if PageID = CDSConfigPage.ID then
  begin
    if SystemDropDown.Text <> CustomMessage('SystemCDS') then
      Result := True;
  end;
end;

// ---------------------------------------------------------------------------------
// NEXT BUTTON VALIDATION
// Validates user input before proceeding to the next page.
// ---------------------------------------------------------------------------------
function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  
  // Note: We do not check IsUpgrade here because ShouldSkipPage
  // ensures we never land on this page during an upgrade.

  if CurPageID = CDSConfigPage.ID then
  begin
    if Trim(CDSFolderInput.Text) = '' then
    begin
      MsgBox(CustomMessage('ErrorFolderRequired'), mbError, MB_OK);
      Result := False;
    end
    else if not DirExists(CDSFolderInput.Text) then
    begin
      MsgBox(CustomMessage('ErrorFolderNotFound'), mbError, MB_OK);
      Result := False;
    end
    else
    begin
      SelectedFolderPath := CDSFolderInput.Text;
    end;
  end;
end;

// ---------------------------------------------------------------------------------
// POST INSTALL STEP
// Handles config file generation after files are installed.
// ---------------------------------------------------------------------------------
procedure CurStepChanged(CurStep: TSetupStep);
var
  TemplatePath, ConfigPath, SelectedSystem: String;
  FileContent: TArrayOfString;
  EscapedPath: String;
  i: Integer;
begin
  if CurStep = ssPostInstall then
  begin
    ConfigPath := ExpandConstant('{app}\resources\config.yml');

    // PROTECTION: If upgrade mode is active but config.yml is missing,
    // we should NOT create a new one because we skipped the UI to configure it.
    if IsUpgrade and (not FileExists(ConfigPath)) then
    begin
       Log('WARNING: Upgrade mode detected but config.yml is missing. Skipping creation to avoid invalid defaults.');
       Exit;
    end;

    // Only create config if it doesn't exist (Standard Install or First Run)
    if not FileExists(ConfigPath) then
    begin
      TemplatePath := ExpandConstant('{app}\resources\config_example.yml');
      Log('config.yml not found. Creating from template...');
      
      SelectedSystem := SystemDropDown.Text;
      
      if LoadStringsFromFile(TemplatePath, FileContent) then
      begin
        for i := 0 to GetArrayLength(FileContent) - 1 do
        begin
          // Replace system placeholder
          FileContent[i] := StringReplace(FileContent[i], '{{ TARGET_SYSTEM }}', SelectedSystem);
          
          // Replace folder path only if system is CDS
          if SelectedSystem = CustomMessage('SystemCDS') then
          begin
            EscapedPath := SelectedFolderPath;
            // Escape backslashes for YAML/JSON compatibility
            StringChangeEx(EscapedPath, '\', '\\', True);
            FileContent[i] := StringReplace(FileContent[i], '{{ FOLDER_TO_WATCH }}', EscapedPath);
          end;
        end;
        
        if SaveStringsToUTF8File(ConfigPath, FileContent, True) then
          Log(Format('Successfully created config.yml with system: %s', [SelectedSystem]))
        else
          Log('Error saving config.yml');
      end
      else
        Log('Error reading template file');
    end
    else
    begin
      Log('config.yml already exists. Preserving settings.');
    end;
  end;
end;