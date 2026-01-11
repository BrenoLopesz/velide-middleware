import functools
import sys
import traceback

class TrayGuard:
    """
    A helper to safely handle tray icon cleanup during crashes.
    Acts as both a decorator and a registry.
    """
    _tray_instance = None

    @classmethod
    def register(cls, tray):
        """Register the tray and hook into the global exception handler."""
        cls._tray_instance = tray
        # Hijack the global exception hook
        sys.excepthook = cls._handle_exception

    @classmethod
    def _handle_exception(cls, exc_type, exc_value, exc_traceback):
        """This function runs automatically on ANY unhandled crash."""
        
        print("--- CRASH DETECTED BY TRAY GUARD ---")
        traceback.print_exception(exc_type, exc_value, exc_traceback)

        # 1. Cleanup the tray icon (prevent ghost icon)
        if cls._tray_instance:
            try:
                cls._tray_instance.cleanup()
            except:
                pass

        try:
            error_msg = "".join(
                traceback.format_exception(
                    exc_type, 
                    exc_value, 
                    exc_traceback
                )
            )
            print(error_msg) # Ensure it's in the logs
        except:
            pass

        # 3. Quit the app properly
        sys.exit(1)

    # Keep this for startup errors (before app.exec_)
    @classmethod
    def cleanup_on_error(cls, func):
        """The decorator to wrap your main function."""
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except Exception as e:
                # 1. Check if a tray was registered before the crash
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