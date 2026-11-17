"""Bounded, redirect-free REST transport for the shared middleware v1 contract.

Authentication contexts are explicit method arguments. No token is stored on the
HTTP session, and public bootstrap never inherits an Authorization header.
"""
import hashlib
import ipaddress
import json
from pathlib import Path
import queue
import threading
import time
from urllib.parse import urlsplit
import uuid

import pytz
import requests
import jwt


MAX_BYTES = 8 * 1024 * 1024


class MiddlewareError(ValueError):
    def __init__(self, code, message, retryable=False, status=None):
        super().__init__(message)
        self.code, self.retryable, self.status = code, retryable, status


def sha256_text(value):
    if not isinstance(value, str):
        raise MiddlewareError("INVALID_RESPONSE", "Expected an exact UTF-8 JSON string")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def uuid_text(value):
    if not isinstance(value, str) or str(uuid.UUID(value)) != value.lower():
        raise ValueError("Invalid UUID")
    return value


def secure_url(value, allow_loopback=False, websocket=False):
    parts = urlsplit(value)
    scheme = "wss" if websocket else "https"
    local = parts.hostname == "localhost"
    try:
        local = local or ipaddress.ip_address(parts.hostname).is_loopback
    except ValueError:
        pass
    if (not parts.hostname or parts.username or parts.password or parts.query or parts.fragment
            or (parts.scheme != scheme and not (
                allow_loopback and local and parts.scheme == ("ws" if websocket else "http")))):
        raise MiddlewareError("DEPLOYMENT_CONFIGURATION", "Configure an explicit secure Middleware service URL")
    return value


def validate_auth(auth):
    if not isinstance(auth, dict) or set(auth) != {"domain", "client_id", "scope", "audience"}:
        raise MiddlewareError("INVALID_TARGET", "Incomplete native authentication configuration")
    if not all(isinstance(value, str) and value.strip() for value in auth.values()):
        raise MiddlewareError("INVALID_TARGET", "Invalid native authentication configuration")
    domain = urlsplit(auth["domain"] if "://" in auth["domain"] else "https://" + auth["domain"])
    if domain.scheme != "https" or not domain.hostname or domain.username or domain.password or domain.query or domain.fragment or domain.path not in ("", "/"):
        raise MiddlewareError("INVALID_TARGET", "Invalid native authentication domain")
    return auth


def has_explicit_grant_scope(bundle):
    """New grants must prove their requested scope, unlike old cache adoption."""
    try:
        token = bundle["access_token"]
        token = token[7:] if token.startswith("Bearer ") else token
        claims = jwt.decode(token, options={"verify_signature": False, "verify_exp": False})
        return isinstance(bundle.get("scope") or claims.get("scope"), str)
    except (KeyError, TypeError, AttributeError, jwt.InvalidTokenError):
        return False


def validate_target(target, allow_loopback=False):
    try:
        if set(target) != {"auth", "api"}:
            raise ValueError()
        validate_auth(target["auth"])
        api = target["api"]
        if set(api) != {"velide_server", "velide_websockets_server", "use_ssl", "native"} or api["use_ssl"] is not True:
            raise ValueError()
        secure_url(api["velide_server"], allow_loopback)
        secure_url(api["velide_websockets_server"], allow_loopback, websocket=True)
        if not all(urlsplit(api[key]).path.rstrip("/").endswith("/graphql")
                   for key in ("velide_server", "velide_websockets_server")):
            raise ValueError()
        native = api["native"]
        if set(native) != {"merchant_id", "connection_id", "timezone"}:
            raise ValueError()
        uuid_text(native["merchant_id"])
        uuid_text(native["connection_id"])
        pytz.timezone(native["timezone"])
    except (ValueError, TypeError, KeyError, AttributeError, pytz.UnknownTimeZoneError) as error:
        raise MiddlewareError("INVALID_TARGET", "Native target configuration is invalid") from error
    return target


def service_url(bundle_dir, allow_loopback=False):
    path = Path(bundle_dir) / "resources" / "middleware-service.json"
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        if document.get("version") != 1 or not document.get("base_url"):
            raise ValueError()
        if (urlsplit(document["base_url"]).hostname or "").endswith(".invalid"):
            raise ValueError("Deployment placeholder is not a service")
        return MiddlewareTransport.validate_base_url(document["base_url"], allow_loopback)
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise MiddlewareError("DEPLOYMENT_CONFIGURATION",
                              "Esta versão não tem o serviço de configuração. Instale uma versão com "
                              "resources/middleware-service.json configurado pela distribuição.") from error


class MiddlewareTransport:
    def __init__(self, base_url, allow_loopback=False, session=None, timeout=(5, 15), total_timeout=20):
        self.base_url = self.validate_base_url(base_url, allow_loopback)
        self.allow_loopback = allow_loopback
        self.session = session or requests.Session()
        self.session.trust_env = False
        self.timeout = timeout
        self.total_timeout = total_timeout
        self.deadline = None
        self._requests = threading.BoundedSemaphore(2)

    @staticmethod
    def validate_base_url(base_url, allow_loopback=False):
        secure_url(base_url, allow_loopback)
        if urlsplit(base_url).path.rstrip("/") != "/api/middleware/v1":
            raise MiddlewareError("DEPLOYMENT_CONFIGURATION", "Middleware base URL must end in /api/middleware/v1")
        return base_url.rstrip("/")

    def request(self, method, path, token=None, body=None):
        # requests' read timeout is an inactivity limit, NOT a whole-operation
        # deadline. A trickling peer must not keep ERP writers paused forever.
        # Only this daemon owns the response; abandoned late results never touch
        # coordinator state. Bound outstanding abandoned requests as well.
        budget = self.total_timeout
        if self.deadline is not None:
            budget = min(budget, self.deadline - time.monotonic())
        if budget <= 0:
            raise MiddlewareError("TIMEOUT", "Middleware request deadline expired", True)
        if not self._requests.acquire(blocking=False):
            raise MiddlewareError("OFFLINE", "Previous Middleware requests are still closing", True)
        completed = queue.Queue(maxsize=1)
        abandoned = threading.Event()
        def run():
            try:
                value = self._request(method, path, token, body, abandoned)
                completed.put((value, None))
            except BaseException as error:
                completed.put((None, error))
            finally:
                self._requests.release()
        threading.Thread(target=run, name="middleware-http", daemon=True).start()
        try:
            result, error = completed.get(timeout=budget)
        except queue.Empty:
            abandoned.set()
            raise MiddlewareError("TIMEOUT", "Middleware request deadline expired", True)
        if error is not None:
            raise error
        return result

    def _request(self, method, path, token=None, body=None, abandoned=None):
        headers = {"Accept": "application/json", "Cache-Control": "no-store", "Authorization": None}
        if token:
            token = token[7:] if token.startswith("Bearer ") else token
            if not token or any(c.isspace() for c in token):
                raise MiddlewareError("INVALID_TOKEN", "Invalid access token")
            headers["Authorization"] = "Bearer " + token
        encoded = None
        if body is not None:
            encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            if len(encoded) > MAX_BYTES:
                raise MiddlewareError("REQUEST_TOO_LARGE", "Inventory exceeds the 8 MiB request limit")
            headers["Content-Type"] = "application/json; charset=utf-8"
        try:
            with self.session.request(method, self.base_url + path, headers=headers, data=encoded,
                                      timeout=self.timeout, allow_redirects=False, stream=True) as response:
                if 300 <= response.status_code < 400:
                    raise MiddlewareError("REDIRECT_REFUSED", "Middleware service redirects are not allowed")
                chunks, size = [], 0
                for chunk in response.iter_content(65536):
                    if abandoned is not None and abandoned.is_set():
                        raise MiddlewareError("TIMEOUT", "Middleware response abandoned", True)
                    size += len(chunk)
                    if size > MAX_BYTES:
                        raise MiddlewareError("RESPONSE_TOO_LARGE", "Middleware response exceeds 8 MiB")
                    chunks.append(chunk)
                data = json.loads(b"".join(chunks).decode("utf-8"))
                if not isinstance(data, dict):
                    raise ValueError()
                if response.status_code >= 400:
                    error = data.get("error", {})
                    raise MiddlewareError(str(error.get("code", "SERVICE_ERROR")),
                                          str(error.get("message", "Middleware service unavailable")),
                                          error.get("retryable") is True, response.status_code)
                if response.status_code != 200:
                    raise MiddlewareError("INVALID_RESPONSE", "Unexpected Middleware response status")
                return data
        except requests.RequestException as error:
            raise MiddlewareError("OFFLINE", "Middleware service unavailable; retry when connected", True) from error
        except (UnicodeError, json.JSONDecodeError) as error:
            raise MiddlewareError("INVALID_RESPONSE", "Malformed Middleware response") from error

    def bootstrap(self):
        result = self.request("GET", "/bootstrap")
        if result.get("version") != 1:
            raise MiddlewareError("INVALID_RESPONSE", "Unsupported Middleware protocol")
        validate_auth(result.get("auth"))
        return result

    def options(self, native_token):
        result = self.request("GET", "/onboarding/options", native_token)
        if not isinstance(result.get("merchants"), list):
            raise MiddlewareError("INVALID_RESPONSE", "Invalid merchant options")
        return result

    def connect(self, native_token, installation_id, merchant_id, source, mode, connection_id=None):
        body = dict(installation_id=uuid_text(installation_id), merchant_id=uuid_text(merchant_id),
                    source=source, mode=mode)
        if mode not in ("create", "reconnect") or source not in ("Farmax", "CDS"):
            raise ValueError("Invalid onboarding source or mode")
        if connection_id is not None:
            body["connection_id"] = uuid_text(connection_id)
        result = self.request("POST", "/onboarding/connect", native_token, body)
        validate_target(result.get("target"), self.allow_loopback)
        if result.get("installation_id") != installation_id or result.get("mode") != mode:
            raise MiddlewareError("INVALID_RESPONSE", "Onboarding response binding changed")
        return result

    def verify(self, native_token, installation_id, merchant_id, connection_id):
        result = self.request("POST", "/native/verify", native_token,
                              dict(installation_id=uuid_text(installation_id), merchant_id=uuid_text(merchant_id),
                                   connection_id=uuid_text(connection_id)))
        validate_target(result.get("target"), self.allow_loopback)
        if result.get("authorized") is not True:
            raise MiddlewareError("FORBIDDEN", "Native connection is not authorized")
        return result

    def discover(self, bootstrap_token, installation_id, source, source_identity):
        result = self.request("POST", "/rollouts/discover", bootstrap_token,
                              dict(installation_id=uuid_text(installation_id), source=source, source_identity=source_identity))
        if result.get("installation_id") != installation_id or result.get("state") not in ("DISABLED", "ENABLED", "APPLIED"):
            raise MiddlewareError("INVALID_RESPONSE", "Invalid rollout discovery response")
        if result.get("target") is not None:
            validate_target(result["target"], self.allow_loopback)
        return result

    def _attempt_path(self, installation_id, attempt_id):
        return "/rollouts/{}/attempts/{}".format(uuid_text(installation_id), uuid_text(attempt_id))

    def submit_inventory(self, bootstrap_token, installation_id, attempt_id, inventory_json, pause_seconds=120):
        return self.request("POST", "/rollouts/{}/attempts".format(uuid_text(installation_id)), bootstrap_token,
                            dict(attempt_id=uuid_text(attempt_id), inventory_json=inventory_json,
                                 inventory_sha256=sha256_text(inventory_json), pause_seconds=pause_seconds))

    def status(self, bootstrap_token, installation_id, attempt_id):
        return self.request("GET", self._attempt_path(installation_id, attempt_id), bootstrap_token)

    def package(self, bootstrap_token, installation_id, attempt_id):
        return self.request("GET", self._attempt_path(installation_id, attempt_id) + "/package", bootstrap_token)

    def cancel(self, bootstrap_token, installation_id, attempt_id, reason):
        return self.request("POST", self._attempt_path(installation_id, attempt_id) + "/cancel", bootstrap_token, {"reason": reason})

    def ack(self, token, installation_id, attempt_id, package_sha256, receipt_id):
        return self.request("POST", self._attempt_path(installation_id, attempt_id) + "/ack", token,
                            dict(package_sha256=package_sha256, receipt_id=uuid_text(receipt_id)))
