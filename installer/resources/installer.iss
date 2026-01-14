; =================================================================================
; MASTER INSTALLER SCRIPT: Velide Middleware
; FILE: installer.iss
;
; =================================================================================
; SCRIPT USAGE AND COMPILATION GUIDE
;
; This acts as the main orchestrator for the Inno Setup build. It includes distinct
; modules for settings, file payload, and Pascal logic to maintain a clean architecture.
;
; This guide explains how to compile this script to generate 32-bit and 64-bit
; installers, and how to run the final executable with different parameters.
; =================================================================================
;
; --- I. Project Structure ---
;
; This installer is split into four modular files located in 'installer/resources/':
;
; 1. installer.iss (This File):
;    The entry point. It orchestrates the build order.
;
; 2. settings.iss:
;    Contains global defines (App Name, Version), Setup directives (Architectures,
;    Privileges), Languages, and Custom Messages.
;
; 3. files_and_run.iss:
;    Contains the [Files] payload, [Icons] shortcuts, and [Run] post-install steps.
;
; 4. logic.pas:
;    Contains the pure Pascal Scripting logic (Functions, Event Handlers).
;
; =================================================================================
;
; --- II. How to Compile 32-bit and 64-bit Installers ---
;
; This script is configured to produce separate installers for 32-bit (x86) and
; 64-bit (x64) architectures. The choice is controlled by the 'MyArch' preprocessor
; variable (defined in settings.iss), which defaults to "x86".
;
; 1. TO COMPILE THE 32-BIT (x86) INSTALLER (Default):
;    -------------------------------------------------
;    Simply compile this script without any special flags.
;
;    Using the Command-Line Compiler (ISCC.exe):
;    > iscc.exe "installer.iss"
;
;    This will produce the 32-bit output file 'velide_install_x86.exe'.
;
; 2. TO COMPILE THE 64-BIT (x64) INSTALLER (Override):
;    --------------------------------------------------
;    You must override the default value of 'MyArch' by passing a definition to the
;    command-line compiler. This is the recommended method for automation.
;
;    Using the Command-Line Compiler (ISCC.exe):
;    > iscc.exe /DMyArch=x64 "installer.iss"
;
;    The `/DMyArch=x64` switch takes precedence over the in-script default,
;    instructing the compiler to build the 64-bit version. This will produce
;    the 64-bit output file 'velide_install_x64.exe'.
;
; =================================================================================
;
; --- III. How to Run the Compiled Installer Executable ---
;
; The generated executable (e.g., 'velide_install_x64.exe') supports several modes.
;
; 1. STANDARD INSTALLATION (INTERACTIVE):
;    ------------------------------------
;    Simply double-click the executable or run it from the command line.
;    > velide_install_x64.exe
;
;    This launches the graphical wizard, allowing the user to make choices on the
;    custom pages (System Selection, CDS Configuration, etc.).
;
; 2. SILENT INSTALLATION:
;    ---------------------
;    Useful for scripted deployments.
;    > velide_install_x64.exe /SILENT /VERYSILENT
;
;    - /SILENT shows installation progress but no wizard pages.
;    - /VERYSILENT shows nothing at all.
;    NOTE: Silent install skips custom pages; config.yml will not be generated
;    unless you implement custom command-line parameter handling for it.
;
; 3. UPGRADE MODE (FOR APPLICATION AUTO-UPDATER):
;    --------------------------------------------
;    This mode is triggered by the custom '/UPDATE=1' command-line parameter.
;    > velide_install_x64.exe /UPDATE=1
;
;    Designed to be called BY YOUR APPLICATION for self-updates:
;    - SKIPS WIZARD PAGES: User is not asked to select system or paths.
;    - PRESERVES CONFIGURATION: It will NOT overwrite an existing 'config.yml'.
;    - TEMP DIRECTORY: It installs to a temporary folder to ensure the update
;      process doesn't conflict with running files.
;
; =================================================================================

; 1. IMPORT SETTINGS
; Contains AppName, Version, Publisher, Architectures, and Text definitions.
#include "settings.iss"

; 2. IMPORT FILES & EXECUTION STEPS
; Contains [Files], [Icons], and [Run] sections.
#include "files_and_run.iss"

; 3. IMPORT PASCAL LOGIC
; Contains the Pascal Scripting functions.
; We declare the [Code] section header here, then include the logic file.
; (Ensure logic.pas contains ONLY functions, not the [Code] header itself).
[Code]
#include "logic.pas"