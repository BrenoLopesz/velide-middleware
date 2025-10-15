@echo off
setlocal

:: ============================================================================
::                Application Update Applicator Script
::
::  - Waits for 3 seconds.
::  - Moves all contents from the UPDATE_SOURCE_FOLDER to the current folder.
::  - Overwrites existing files.
::  - Excludes this script from being moved/overwritten.
::  - Deletes the source folder after a successful move.
:: ============================================================================

:: --- CONFIGURATION ---
:: Set this to the name of the folder containing the new update files.
set "UPDATE_SOURCE_FOLDER=%TEMP%\velide_installer_update"

:: Set the main executable to run after the update is finished.
set "MAIN_EXECUTABLE=..\..\main.exe"

:: --- SCRIPT LOGIC (Do not change below this line) ---
title Applying Update...
cls

echo.
echo =================================================
echo             APPLYING APPLICATION UPDATE
echo =================================================
echo.
echo This script will move new files from the '%UPDATE_SOURCE_FOLDER%' folder
echo into the current directory.
echo.
echo Starting in 4 seconds...
echo.

:: 1. Initial 4-second sleep time.
timeout /t 4 /nobreak >nul

:: Get the path of the current directory and the name of this script.
set "DESTINATION_FOLDER=%~dp0"
set "SELF_SCRIPT_NAME=%~nx0"

:: 2. Check if the source folder exists before proceeding.
if not exist "%UPDATE_SOURCE_FOLDER%" (
    echo [ERROR] The source folder "%UPDATE_SOURCE_FOLDER%" was not found.
    echo Update cannot be applied.
    goto End
)

echo Moving files from: "%UPDATE_SOURCE_FOLDER%"
echo Moving files to:   "%DESTINATION_FOLDER%"
echo Excluding self:    "%SELF_SCRIPT_NAME%"
echo.
echo Please wait, this may take a moment...
echo.

:: 3. Use robocopy to move all files and folders.
:: /E      -> Copies subdirectories, including empty ones.
:: /MOVE   -> Moves files & dirs (deletes from source after copying).
:: /IS     -> Includes "same" files. This forces an overwrite even if timestamp/size are identical.
:: /XF ... -> Excludes the specified file (our own .bat script).
robocopy %UPDATE_SOURCE_FOLDER% %DESTINATION_FOLDER% /E /MOVE /IS /XF %SELF_SCRIPT_NAME%

:: Robocopy exit codes 0-7 are success. 8 and above are failures.
if %ERRORLEVEL% GEQ 8 (
    echo.
    echo [ERROR] An error occurred during the file move process.
    echo The update may not have been applied correctly.
    goto End
)

:: 4. Clean up the now-empty source folder. Robocopy with /MOVE should
:: already do this, but this is a fallback for safety.
if exist "%UPDATE_SOURCE_FOLDER%" (
    rmdir "%UPDATE_SOURCE_FOLDER%"
)

echo.
echo =================================================
echo      Update applied successfully!
echo =================================================
echo.

:: 5. NEW SECTION - Restart the main application.
if defined MAIN_EXECUTABLE (
    set "AppPath=%~dp0%MAIN_EXECUTABLE%"
    echo Checking for application: "%AppPath%"
    
    if exist "%AppPath%" (
        echo Restarting application...
        :: Use start to launch the app independently of the cmd window.
        :: /D sets the "Start In" directory, which is crucial for apps
        :: that need to find their own files (configs, DLLs, etc.).
        start "Restarting Application" /D "%~dp0" "%AppPath%"
        exit /b
    ) else (
        echo [WARNING] Could not find '%MAIN_EXECUTABLE%' to restart.
        goto End
    )
)

:End
echo Press any key to close this window.
pause >nul
exit /b