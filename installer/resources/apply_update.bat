@echo off
setlocal


:: ============================================================================
::                      Application Update Applicator Script
::
::  - Waits for a few seconds.
::  - Moves all contents from the UPDATE_SOURCE_FOLDER to the current folder.
::  - Overwrites existing files.
::  - Excludes this script from being moved/overwritten.
::  - Deletes the source folder after a successful move.
::  - Logs all operations to a log file.
:: ============================================================================

:: --- CONFIGURATION ---
:: Set this to the name of the folder containing the new update files.
set "UPDATE_SOURCE_FOLDER=%TEMP%\velide_installer_update"

:: Set the main executable to run after the update is finished.
set "MAIN_EXECUTABLE=..\..\main.exe"

:: Set the path for the log file. All steps will be appended here.
set "LOG_FILE=%~dp0update_log.txt"


:: --- SCRIPT LOGIC (Do not change below this line) ---
title Applying Update...
cls

:: 0. Log script start
echo =================================================
echo UPDATE SCRIPT STARTED
echo =================================================
echo.
echo Configuration:
echo   - Source Folder: %UPDATE_SOURCE_FOLDER%
echo   - Main Executable: %MAIN_EXECUTABLE%
echo   - Log File: %LOG_FILE%
echo.


echo This script will move new files from the '%UPDATE_SOURCE_FOLDER%' folder
echo into the current directory.
echo.
echo Starting in 5 seconds...
echo.

:: 1. Initial 4-second sleep time.
timeout /t 5 /nobreak >nul

:: Get the path of the current directory and the name of this script.
:: Get the path of the current directory and the name of this script.
set "DESTINATION_FOLDER=%~dp0..\"
:: Remove the trailing backslash from the path to prevent quoting errors.
if "%DESTINATION_FOLDER:~-1%"=="\" set "DESTINATION_FOLDER=%DESTINATION_FOLDER:~0,-1%"

set "SELF_SCRIPT_NAME=%~nx0"
echo Destination folder set to: %DESTINATION_FOLDER%
echo This script's name is: %SELF_SCRIPT_NAME%


:: 2. Check if the source folder exists before proceeding.
echo Checking for source folder...
if not exist "%UPDATE_SOURCE_FOLDER%" (
    echo "[ERROR] The source folder ""%UPDATE_SOURCE_FOLDER%"" was not found."
    echo "Update cannot be applied."
    goto End
)
echo Source folder found.
echo.

echo Preparing to move files...
echo   - From: ""%UPDATE_SOURCE_FOLDER%""
echo   - To:   ""%DESTINATION_FOLDER%"""
echo   - Excluding: ""%SELF_SCRIPT_NAME%"""
echo.
echo Please wait, this may take a moment...
echo.

:: 3. Use robocopy to move all files and folders.
:: /E      -> Copies subdirectories, including empty ones.
:: /MOVE   -> Moves files & dirs (deletes from source after copying).
:: /IS     -> Includes "same" files. This forces an overwrite even if timestamp/size are identical.
:: /XF ... -> Excludes the specified file (our own .bat script).
robocopy "%UPDATE_SOURCE_FOLDER%" "%DESTINATION_FOLDER%" /E /MOVE /IS /XF "%SELF_SCRIPT_NAME%"

:: Robocopy exit codes 0-7 are success. 8 and above are failures.
if %ERRORLEVEL% GEQ 8 (
    echo.
    echo [ERROR] Robocopy failed with exit code %ERRORLEVEL%. An error occurred during the file move process.
    echo The update may not have been applied correctly.
    goto End
)
echo Robocopy completed successfully (Exit Code: %ERRORLEVEL%).

echo %UPDATE_SOURCE_FOLDER%

:: 4. Clean up the now-empty source folder. Robocopy with /MOVE should
:: already do this, but this is a fallback for safety.
if exist "%UPDATE_SOURCE_FOLDER%" (
    echo Attempting to remove source folder as a fallback...
    rmdir "%UPDATE_SOURCE_FOLDER%"
    if exist "%UPDATE_SOURCE_FOLDER%" (
		echo [WARNING] Could not remove source folder: %UPDATE_SOURCE_FOLDER%
    ) else (
        echo Source folder removed successfully.
    )
)

echo.
echo =================================================
echo   Update applied successfully!
echo =================================================
echo.

:: 5. Restart the main application.
if defined MAIN_EXECUTABLE (
    set "AppPath=%~dp0%MAIN_EXECUTABLE%"
    echo Checking for application to restart at: ""%AppPath%""
    
    if exist "%AppPath%" (
        echo Application found. Restarting...
        :: Use start to launch the app independently of the cmd window.
        :: /D sets the "Start In" directory, which is crucial for apps
        :: that need to find their own files (configs, DLLs, etc.).
        start "Restarting Application" /D "%~dp0" "%AppPath%" --is-update-checked
        echo Start command issued for ""%AppPath%""
        goto End
    ) else (
        echo [WARNING] Could not find '%MAIN_EXECUTABLE%' to restart.
        goto End
    )
)

:End