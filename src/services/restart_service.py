import sys
from PyQt5.QtCore import QCoreApplication, QProcess


def restart_application(self):
    """
    Restarts the current application.
    """
    # 1. Get the current application instance
    app = QCoreApplication.instance()

    # 2. Schedule the application to quit
    # This will ensure all cleanup is done correctly
    app.quit()

    # 3. Start a new detached process of the same application
    # sys.executable is the path to the python interpreter
    # sys.argv is the list of command-line arguments, 
    # with sys.argv[0] being the script name
    QProcess.startDetached(sys.executable, sys.argv)
