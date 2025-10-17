import logging
import logging.handlers
import os

from utils.bundle_dir import BUNDLE_DIR
from utils.log_handler import QLogHandler

def setup_logging(log_handler: QLogHandler):
    """
    Configures the logging system.

    This setup includes:
    1. A console handler for real-time output (DEBUG and above).
    2. A timed rotating file handler for general application logs (INFO and above),
       which creates a new file each day and keeps the last 7 days' logs.
       This handler uses a special format for INFO-level logs.
    3. A separate timed rotating file handler specifically for error logs (ERROR and above),
       also rotating daily and keeping 7 days of history.
    """
    # Create logs directory if it doesn't exist
    logs_folder = os.path.join(BUNDLE_DIR, 'resources','logs')
    if not os.path.exists(logs_folder):
        os.makedirs(logs_folder)

    # Get the root logger
    logger = logging.getLogger()
    logger.setLevel(logging.DEBUG) # Set the lowest level to capture everything

    # Create formatters
    default_formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    # Create the level-based formatter for the general app log file.
    # INFO logs will have a user-friendly format without the logger name.
    app_log_formatter = logging.Formatter(
        '%(asctime)s - %(levelname)s - %(message)s'
    )

    # 1. Console Handler (for DEBUG and above)
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.DEBUG)
    console_handler.setFormatter(default_formatter) # Use the standard default formatter
    logger.addHandler(console_handler)

    log_handler.setLevel(logging.INFO)
    logger.addHandler(log_handler)

    # 2. Timed Rotating File Handler for general logs (INFO and above)
    # This will create a new log file every day at midnight.
    # It will keep the logs for the last 7 days (backupCount=7).
    info_log_path = os.path.join(logs_folder, 'app.log')
    info_handler = logging.handlers.TimedRotatingFileHandler(
        info_log_path, when='midnight', interval=1, backupCount=7
    )
    info_handler.setLevel(logging.INFO)
    info_handler.setFormatter(app_log_formatter) # Use the custom, level-based formatter
    logger.addHandler(info_handler)

    # 3. Timed Rotating File Handler for error logs (ERROR and above)
    # This also rotates daily and keeps 7 days of history.
    error_log_path = os.path.join(logs_folder, 'error.log')
    error_handler = logging.handlers.TimedRotatingFileHandler(
        error_log_path, when='midnight', interval=1, backupCount=7
    )
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(default_formatter) # Use the standard default formatter
    logger.addHandler(error_handler)

    logging.debug("Setup do logging está completo.")