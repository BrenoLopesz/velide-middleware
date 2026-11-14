"""Bind a legacy cached session before changing its installed auth configuration.

The app instance/config upgrade locks must already be held, with auth writers
stopped. This is a cutover-only operation. It neither validates access on the
server nor changes tokens. The worker owns cache eligibility/signature handling.
"""
import base64
import binascii
import json
import os
from pathlib import Path
import tempfile


def _temporary_file(token_path, purpose, suffix):
    descriptor, name = tempfile.mkstemp(dir=str(token_path.parent), prefix=token_path.name + purpose, suffix=suffix)
    os.close(descriptor)
    return Path(name)


def _write_durable(path, contents):
    with path.open("wb") as stream:
        stream.write(contents)
        stream.flush()
        os.fsync(stream.fileno())


def bind_pre_cutover_auth(token_path, previous_auth, backups):
    """Attach exact PRE-cutover model values to an unbound base64-JSON bundle.

    Returns True only when a bundle was updated. Missing/empty caches and already
    matching metadata need no write. Contradictory existing metadata is never
    relabelled. Backup and token publication must finish before config publication.

    If the later config replacement fails, this metadata still matches the old
    configuration: both its access token and refresh token remain usable there.
    """
    token_path = Path(token_path)
    try:
        original = token_path.read_bytes()
    except FileNotFoundError:
        return False
    if not original.strip():
        return False
    try:
        # The old reader accepts wrapped base64; retain that compatibility while
        # rejecting malformed bundles without exposing token contents in errors.
        decoded = base64.b64decode(b"".join(original.split()), validate=True)
        bundle = json.loads(decoded.decode("utf-8"))
    except (ValueError, UnicodeError, binascii.Error) as error:
        raise ValueError("Could not read the stored session before native cutover") from error
    if not isinstance(bundle, dict) or not isinstance(bundle.get("access_token"), str):
        raise ValueError("Invalid stored session bundle before native cutover")
    # Match workers.stored_token_retriever_worker.auth_context exactly. Values
    # come from the already validated OLD Settings.auth, never the proposed YAML
    # or token scope claims (original JWTs may contain no scope claim).
    context = {name: getattr(previous_auth, name) for name in ("domain", "client_id", "audience", "scope")}
    existing = bundle.get("middleware_auth")
    if existing is not None:
        if existing != context:
            raise ValueError("Stored session is bound to a different authentication context")
        return False
    from workers.stored_token_retriever_worker import stored_session_matches
    if not stored_session_matches(bundle, previous_auth):
        # Metadata must not turn a cache that the old context would reject into
        # an eligible one. This check deliberately does not require scope claims.
        raise ValueError("Unbound stored session does not match the pre-cutover authentication context")
    bundle["middleware_auth"] = context
    encoded = base64.b64encode(json.dumps(bundle, ensure_ascii=True).encode("utf-8"))
    backup = _temporary_file(token_path, ".pre-native-auth-", ".bak")
    try:
        _write_durable(backup, original)
    except BaseException:
        backup.unlink()
        raise
    backups.append(str(backup))
    candidate = _temporary_file(token_path, ".bind-auth-", ".tmp")
    try:
        _write_durable(candidate, encoded)
        if token_path.read_bytes() != original:
            raise ValueError("Stored session changed during native cutover preparation")
        os.replace(str(candidate), str(token_path))
    finally:
        if candidate.exists():
            candidate.unlink()
    return True
