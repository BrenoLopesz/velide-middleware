"""Build-only workaround for pathlib wildcard stat failures on Python 3.8.0."""
import glob
import sys


def install_qt_glob_compat():
    if sys.platform != "win32" or sys.version_info[:3] != (3, 8, 0):
        return
    from cx_Freeze.hooks import _qthooks

    def get_files(name, variable, argument):
        source, target = _qthooks.get_qt_paths(name, variable)
        # Do not stat "Qt*.dll": Windows rejects it instead of returning False
        # in this supported Python patch release. Expansion belongs to glob.
        if not glob.has_magic(argument) and (source / argument).is_dir():
            source, target, argument = source / argument, target / argument, "*"
        return [(path, target / path.name) for path in source.glob(argument)]

    _qthooks._get_qt_files = get_files
