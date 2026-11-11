from hashlib import sha256


def cds_file_identity(filename, created_at):
    """File reuse policy: one receipt per filename and original order timestamp."""
    return "cds-" + sha256((filename.lower() + "\0" + created_at.isoformat()).encode("utf-8")).hexdigest()
