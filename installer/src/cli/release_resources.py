"""Explicit application assets: never freeze a workstation DB/config/test export."""
import argparse
import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit


UI_RESOURCES = (
    "error.png", "loading.png", "style.css", "velide.png", "velide_white.png",
    "velide_36.png", "velide.ico", "velide_corner.ico",
    "fonts/font_regular.ttf", "fonts/font_light.ttf", "fonts/font_bold.ttf",
)
SERVICE_RESOURCE = "middleware-service.json"
SERVICE_URL_ENV = "MIDDLEWARE_SERVICE_BASE_URL"
MAIN_RESOURCES = ("config_example.yml", SERVICE_RESOURCE) + UI_RESOURCES
UPDATER_RESOURCES = (
    "config.yml", "private_public.pem", "apply_update.bat", "installer.iss",
    "files_and_run.iss", "logic.pas", "settings.iss",
) + UI_RESOURCES


def deployment_base_url(value):
    """Validate syntax offline. Deployment ownership/TLS are release inputs."""
    error = (
        SERVICE_URL_ENV + " must be an explicit HTTPS deployment URL with a "
        "public DNS hostname and the exact path /api/middleware/v1 "
        "(no credentials, query, fragment, or placeholder hostname)"
    )
    if not isinstance(value, str) or not value or len(value) > 2048:
        raise ValueError(error)
    if any(ord(char) <= 32 or ord(char) >= 127 for char in value):
        raise ValueError(error)
    try:
        url = urlsplit(value)
        host = url.hostname or ""
        port = url.port
    except ValueError:
        raise ValueError(error) from None
    labels = host.split(".")
    reserved = {
        "localhost", "local", "internal", "invalid", "test", "example",
        "lan", "home", "onion",
    }
    if (
        url.scheme != "https" or not value.startswith("https://")
        or url.path != "/api/middleware/v1"
        or url.username is not None or url.password is not None
        or "?" in value or "#" in value or "\\" in value
        or port == 0 or url.netloc.endswith(":")
        or len(host) > 253 or len(labels) < 2
        or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
               for label in labels)
        or not re.fullmatch(r"[a-z]{2,63}", labels[-1])
        or labels[-1] in reserved
        or any(host == domain or host.endswith("." + domain)
               for domain in ("example.com", "example.net", "example.org"))
    ):
        raise ValueError(error)
    return value


def generate_service_resource(destination):
    """Always require the release setting, even if a previous build exists."""
    base_url = deployment_base_url(os.environ.get(SERVICE_URL_ENV))
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    pending = destination.with_suffix(".pending")
    pending.write_text(
        json.dumps({"version": 1, "base_url": base_url}, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(str(pending), str(destination))
    return destination


def resource_files(directory, updater=False, service_resource=None):
    root = Path(directory)
    assets = UPDATER_RESOURCES if updater else MAIN_RESOURCES
    result = []
    for name in assets:
        source = (Path(service_resource)
                  if name == SERVICE_RESOURCE and service_resource is not None
                  else root / name)
        if not source.is_file():
            raise FileNotFoundError("Required release asset is missing: " + str(source))
        result.append((str(source), "resources/" + name))
    return result


def check_frozen_resources(directory, updater=False):
    """Reject stale/customer assets before compiling or signing a release."""
    root = Path(directory)
    assets = UPDATER_RESOURCES if updater else MAIN_RESOURCES
    expected = set(assets)
    actual = {
        path.relative_to(root / "resources").as_posix()
        for path in (root / "resources").rglob("*") if path.is_file()
    }
    if actual != expected:
        raise ValueError("Frozen resources differ from the release allowlist")
    # cx_Freeze runtime directories are separate from application-owned resources.
    # Unknown root files/directories fail closed rather than entering Inno's payload.
    runtime_dirs = {"lib", "share", "PyQt5.uic.widget-plugins"}
    executables = {"main.exe"} if updater else {"main.exe", "migrate-native.exe"}
    for path in root.iterdir():
        if path.is_dir() and path.name in runtime_dirs | {"resources"}:
            continue
        if path.is_file() and (
            path.name in executables | {"frozen_application_license.txt"}
            or path.suffix.lower() in {".dll", ".pyd"}
        ):
            continue
        raise ValueError("Unexpected frozen payload entry: " + path.name)
    for name in executables:
        if not (root / name).is_file():
            raise FileNotFoundError("Required frozen executable is missing: " + name)
    if not updater:
        document = json.loads(
            (root / "resources" / SERVICE_RESOURCE).read_text(encoding="utf-8")
        )
        if (
            not isinstance(document, dict)
            or set(document) != {"version", "base_url"}
            or type(document["version"]) is not int or document["version"] != 1
            or deployment_base_url(document["base_url"])
            != deployment_base_url(os.environ.get(SERVICE_URL_ENV))
        ):
            raise ValueError("Frozen service resource does not match release setting")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    generate = sub.add_parser("generate")
    generate.add_argument("destination", type=Path)
    check = sub.add_parser("check")
    check.add_argument("directory", type=Path)
    check.add_argument("--updater", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "generate":
            generate_service_resource(args.destination)
        else:
            check_frozen_resources(args.directory, updater=args.updater)
    except (ValueError, OSError) as exc:
        parser.exit(2, str(exc) + "\n")


if __name__ == "__main__":
    main()
