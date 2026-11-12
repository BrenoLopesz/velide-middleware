from hashlib import sha256
import json
import os


def cds_file_identity(filename, created_at):
    """File reuse policy: one receipt per filename and original order timestamp."""
    return "cds-" + sha256((filename.lower() + "\0" + created_at.isoformat()).encode("utf-8")).hexdigest()


def configured_source_identity(settings):
    """Detect configuration changes; this is NOT proof of database incarnation."""
    if settings.target_system.value == "Farmax" and settings.farmax:
        identity = ["Farmax", settings.farmax.host.lower(), settings.farmax.file]
    elif settings.target_system.value == "CDS" and settings.folder_to_watch:
        identity = ["CDS", os.path.normcase(os.path.abspath(settings.folder_to_watch))]
    else:
        return None
    return sha256(json.dumps(identity).encode("utf-8")).hexdigest()
