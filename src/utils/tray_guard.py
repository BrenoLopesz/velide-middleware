import sys
import logging
import traceback

logger = logging.getLogger(__name__)

class TrayGuard:
    """
    A helper to safely handle tray icon cleanup during crashes.
    Acts as both a decorator and a registry.
    """
    _tray_instance = None

    @classmethod
    def register(cls, tray):
        """Register the tray and hook into global exception handlers."""
        cls._tray_instance = tray
        
        # 1. Catch crashes in the Main Thread / GUI Loop
        sys.excepthook = cls._handle_exception
        
        # 2. Catch crashes in Python Threads (threading.Thread)
        # Note: This works for standard threads. QThreads/QRunnables might need their own try/except.
        if sys.version_info >= (3, 8):
            import threading
            threading.excepthook = cls._handle_thread_exception

    @classmethod
    def _handle_exception(cls, exc_type, exc_value, exc_traceback):
        """Handles Main Thread crashes."""
        cls._log_and_exit(exc_type, exc_value, exc_traceback, source="MainLoop")

    @classmethod
    def _handle_thread_exception(cls, args):
        """Handles Background Thread crashes (Python 3.8+)."""
        # args.exc_type, args.exc_value, args.exc_traceback, args.thread
        cls._log_and_exit(args.exc_type, args.exc_value, args.exc_traceback, source="Thread")

    @classmethod
    def _log_and_exit(cls, exc_type, exc_value, exc_traceback, source="Unknown"):
        """Common logic to log, clean up, and quit."""
        
        # 1. LOG THE ERROR (This writes to your error.log)
        logger.critical(
            f"Crash detectado em {source}!", 
            exc_info=(exc_type, exc_value, exc_traceback)
        )
        
        print(f"--- CRASH DETECTED ({source}) ---")
        traceback.print_exception(exc_type, exc_value, exc_traceback)

        # 2. Cleanup Tray
        if cls._tray_instance:
            try:
                cls._tray_instance.cleanup()
            except Exception:
                pass

        # 3. Force Exit
        # We use os._exit(1) to kill the process instantly, preventing
        # error loops if the logging itself failed.
        import os
        os._exit(1)

    @classmethod
    def cleanup_on_error(cls, func):
        """Decorator for the main() function entry point."""
        import functools
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except Exception as e:
                # Capture synchronous errors inside main() before loop starts
                logger.critical("Um crash ocorreu durante a execução do sistema.", exc_info=True)
                if cls._tray_instance:
                    print(f"Exception caught in {func.__name__}! Cleaning up tray...")
                    
                    # Call the cleanup method (assuming your tray class has one)
                    # If using standard QSystemTrayIcon, use .hide()
                    if hasattr(cls._tray_instance, 'cleanup'):
                        cls._tray_instance.cleanup()
                    else:
                        cls._tray_instance.hide()
                        
                # 2. Re-raise the exception so Python can print the stack trace
                raise e
        return wrapper