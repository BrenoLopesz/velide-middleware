; =================================================================================
; FILE: settings.iss
; DESCRIPTION: Contains global settings, preprocessor definitions, and setup directives.
;
; USAGE:
;   - Defines the application metadata (Name, Version, Publisher).
;   - Configures the 32-bit (x86) vs 64-bit (x64) build targets.
;   - Sets up the UI languages and custom messages.
; =================================================================================

; --- ARCHITECTURE CONFIGURATION ---
; To build x64, pass "/DMyArch=x64" to ISCC.exe. Default is "x86".
#ifndef MyArch
  #define MyArch "x86"
#endif

; --- APPLICATION METADATA ---
#define MyAppName "Velide Middleware"
#define MyAppPublisher "Velide"
#define MyAppURL "https://velide.com.br/"
#define MyAppExeName "main.exe"

#ifndef MyAppVersion
  #define MyAppVersion "v3.0.0"
#endif

[Setup]
; NOTE: The AppId uniquely identifies this application. 
; Do not change it for updates to the same application.
AppId={{E1B380E2-6B2C-4153-9C3B-EEA96A13B92E}

AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}

; --- INSTALLATION DIRECTORY ---
; "{autopf}" automatically maps to "Program Files" (x64) or "Program Files (x86)"
; depending on the architecture mode.
DefaultDirName={autopf}\{#MyAppName}
UninstallDisplayIcon={app}\{#MyAppExeName}

; --- INTERFACE SETTINGS ---
DisableProgramGroupPage=yes
SetupIconFile=..\resources\velide.ico
SolidCompression=yes
WizardStyle=modern

; --- PERMISSIONS & COMPATIBILITY ---
; "lowest" allows installation without UAC elevation if writing to user-writable paths.
; Since we default to Program Files, Inno Setup will likely prompt for Admin anyway.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=commandline

; Force Windows 7 SP1 or newer to avoid crashes on unpatched systems.
MinVersion=6.1sp1

; --- ARCHITECTURE SPECIFIC SETTINGS ---
#if MyArch == "x64"
  ; 64-bit Build Settings
  ArchitecturesInstallIn64BitMode=x64compatible
  ArchitecturesAllowed=x64compatible
  OutputBaseFilename=velide_install_x64
#else
  ; 32-bit Build Settings (Default)
  ArchitecturesAllowed=x86compatible
  OutputBaseFilename=velide_install_x86
#endif

; output directory relative to the main script location
OutputDir=..\..\

[Languages]
Name: "en"; MessagesFile: "compiler:Default.isl"
Name: "br"; MessagesFile: "compiler:Languages\BrazilianPortuguese.isl"
Name: "es"; MessagesFile: "compiler:Languages\Spanish.isl"

[CustomMessages]
; --- Page Title & Description ---
en.IntegrationConfigTitle=Integration Configuration
br.IntegrationConfigTitle=Configuração da Integração
es.IntegrationConfigTitle=Configuración de Integración

en.IntegrationConfigDescription=Please, select your system.
br.IntegrationConfigDescription=Por favor, selecione seu sistema.
es.IntegrationConfigDescription=Por favor, seleccione su sistema.

en.IntegrationConfigCaption=Select the system you use and which you want to connect to Velide:
br.IntegrationConfigCaption=Selecione o sistema que você utiliza e que deseja integrar com o Velide:
es.IntegrationConfigCaption=Selecciona el sistema que utilizas y al que quieres conectarte con Velide:

; --- System Dropdown Options ---
en.SystemCDS=CDS
br.SystemCDS=CDS
es.SystemCDS=CDS

en.SystemFarmax=Farmax
br.SystemFarmax=Farmax
es.SystemFarmax=Farmax

; --- CDS Configuration Page ---
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

; --- Farmax Configuration Page ---
en.FarmaxConfigTitle=Farmax Configuration
br.FarmaxConfigTitle=Configuração do Farmax
es.FarmaxConfigTitle=Configuración de Farmax

en.FarmaxConfigDescription=Configure database connection settings.
br.FarmaxConfigDescription=Configure as configurações de conexão com o banco de dados.
es.FarmaxConfigDescription=Configure los ajustes de conexión a la base de datos.

en.FarmaxHostLabel=Host Address:
br.FarmaxHostLabel=Endereço do Host:
es.FarmaxHostLabel=Dirección del Host:

en.FarmaxFileLabel=Database File (.fdb):
br.FarmaxFileLabel=Arquivo do Banco de Dados (.fdb):
es.FarmaxFileLabel=Archivo de Base de Datos (.fdb):

en.FarmaxUserLabel=User:
br.FarmaxUserLabel=Usuário:
es.FarmaxUserLabel=Usuario:

en.FarmaxPasswordLabel=Password:
br.FarmaxPasswordLabel=Senha:
es.FarmaxPasswordLabel=Contraseña:

en.FarmaxSelectDBTitle=Select Firebird Database
br.FarmaxSelectDBTitle=Selecione o Banco de Dados Firebird
es.FarmaxSelectDBTitle=Seleccionar Base de Datos Firebird

en.FarmaxErrorMissing=Please fill in all Farmax connection details.
br.FarmaxErrorMissing=Por favor, preencha todos os detalhes de conexão do Farmax.
es.FarmaxErrorMissing=Por favor, complete todos los detalles de conexión de Farmax.

; --- Common Controls ---
en.SelectFolder=Select folder:
br.SelectFolder=Selecione uma pasta:
es.SelectFolder=Seleccione una carpeta:

en.BrowseCaption=Browse...
br.BrowseCaption=Procurar...
es.BrowseCaption=Explorar...

; --- Error Messages ---
en.ErrorFolderRequired=You must select a folder.
br.ErrorFolderRequired=Você deve selecionar uma pasta.
es.ErrorFolderRequired=Debe seleccionar una carpeta.

en.ErrorFolderNotFound=The selected folder does not exist.
br.ErrorFolderNotFound=A pasta selecionada não existe.
es.ErrorFolderNotFound=La carpeta seleccionada no existe.

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "startup"; Description: "{cm:AutoStartProgram,{#MyAppName}}"; GroupDescription: "{cm:AutoStartProgramGroupDescription}"; Flags: unchecked