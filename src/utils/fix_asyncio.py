import sys
import asyncio

# This file contains the fix for the "ValueError: set_wakeup_fd 
# only works in main thread" error
# that occurs on Python 3.8 and Windows 7


def apply_asyncio_fix():
    """
    Apply the fix for the "ValueError: set_wakeup_fd only works in main thread" error
    that occurs on Python 3.8 and Windows 7.

    This fix should be applied at the start of the application 
    before any asyncio code runs.
    """
    if sys.platform == "win32" and (3, 8, 0) <= sys.version_info < (3, 9, 0):
        # Use the WindowsSelectorEventLoopPolicy instead 
        # of the default WindowsProactorEventLoopPolicy
        # This avoids the issue with set_wakeup_fd in non-main threads
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
