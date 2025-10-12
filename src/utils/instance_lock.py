import os
import sys
import atexit
import msvcrt  # Windows-specific module for file locking
from utils.bundle_dir import BUNDLE_DIR
from PyQt5.QtWidgets import QMessageBox

LOCK_FILE_PATH = os.path.join(BUNDLE_DIR, 'resources', 'velide.lock')
_lock_file_handle = None

def acquire_lock():
    """
    Attempts to acquire an exclusive lock on the lock file.

    If the lock is acquired successfully, it registers a cleanup function
    to release the lock on exit.

    If the lock cannot be acquired (another instance is running), it shows
    an error message and exits the application.
    """
    global _lock_file_handle
    try:
        # Open the file in binary read/write mode, create if it doesn't exist
        _lock_file_handle = os.open(LOCK_FILE_PATH, os.O_RDWR | os.O_CREAT)

        # Attempt to acquire an exclusive, non-blocking lock
        msvcrt.locking(_lock_file_handle, msvcrt.LK_NBLCK, 1)

        # If lock acquired successfully, register the release function
        atexit.register(release_lock)
        return True

    except IOError as e:
        # If locking fails (errno 13: Permission denied, often means locked)
        # or any other IOError occurs during locking attempt
        if e.errno == 13 or isinstance(e, BlockingIOError):
            show_already_running_message()
            if _lock_file_handle is not None:
                os.close(_lock_file_handle)
            sys.exit(1) # Exit indicating an error
        else:
            # Handle other potential IOErrors during file open/lock
            print(f"Unexpected error acquiring lock: {e}") # Log or show a different error
            if _lock_file_handle is not None:
                os.close(_lock_file_handle)
            sys.exit(1) # Exit indicating an error
    except Exception as e:
        # Catch any other unexpected exceptions
        print(f"An unexpected error occurred during lock acquisition: {e}")
        if _lock_file_handle is not None:
            os.close(_lock_file_handle)
        sys.exit(1) # Exit indicating an error


def release_lock():
    """
    Releases the lock and closes the lock file handle.
    This function is registered by atexit to run on normal program termination.
    """
    global _lock_file_handle
    if _lock_file_handle is not None:
        try:
            # Unlock the file region
            msvcrt.locking(_lock_file_handle, msvcrt.LK_UNLCK, 1)
            # Close the file handle
            os.close(_lock_file_handle)
            _lock_file_handle = None
            # Optionally, remove the lock file on clean exit
            # try:
            #     os.remove(LOCK_FILE_PATH)
            # except OSError:
            #     pass # Ignore errors if file couldn't be removed
        except Exception as e:
            # Log error during release if necessary
            print(f"Error releasing lock: {e}")


def show_already_running_message():
    """
    Displays a simple message box informing the user that another instance
    is already running. Requires QApplication to be initialized if used
    after app creation, but we aim to call acquire_lock *before* it.
    If called before QApplication, it might not display correctly or at all.
    Consider a simpler print statement or a platform-specific message box
    if QApplication isn't guaranteed.
    """
    # Using QMessageBox might be problematic if QApplication isn't initialized.
    # A simple print might be more reliable if called very early.
    print("Error: Another instance of vel2farmax is already running.")
    # msgBox = QMessageBox()
    # msgBox.setIcon(QMessageBox.Warning)
    # msgBox.setWindowTitle("Application Already Running")
    # msgBox.setText("Another instance of vel2farmax is already running.")
    # msgBox.setStandardButtons(QMessageBox.Ok)
    # msgBox.exec_()