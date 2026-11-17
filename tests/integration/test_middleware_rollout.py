"""Real inventory/consumer tests; only the remote HTTP service and graph are doubles."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import sqlite3
import time

import jwt
import pytest
import yaml

from local_upgrade import upgrade_installation
from middleware_rollout import RolloutCoordinator, read_state, recover_local_rollout
from middleware_transport import MiddlewareError, sha256_text
from test_automatic_upgrade import installed, package_for, rows
from utils.token_storage import read_token_from_file, store_token_at_file


class Graph:
    def __init__(self):
        self.paused, self.resumed, self.released = False, 0, 0

    def quiesce(self, deadline):
        self.paused = True

    def resume(self):
        self.paused = False
        self.resumed += 1

    def release(self):
        self.released += 1


class Server:
    allow_loopback = False

    def __init__(self, config, database, adopted):
        self.config, self.database, self.adopted = config, database, adopted
        package = package_for(config, database, adopted)
        self.target = {"api": dict(package["configuration"]["api"], use_ssl=True),
                       "auth": {"domain": "native-auth.invalid", "client_id": "native-client",
                                "scope": "openid offline_access", "audience": "native-api"}}
        self.fail_ack = self.offline = False
        self.corruption = None
        self.calls = []

    def discover(self, token, installation, source, identity):
        self.calls.append(("discover", token))
        return dict(installation_id=installation, state="ENABLED", target=self.target, retry_after_seconds=30)

    def verify(self, token, installation, merchant, connection):
        self.calls.append(("verify", token))
        return dict(authorized=True, target=self.target)

    def submit_inventory(self, token, installation, attempt, inventory_json, pause_seconds):
        self.inventory = json.loads(inventory_json)
        self.inventory_hash = sha256_text(inventory_json)
        self.attempt, self.installation = attempt, installation
        self.package_doc = package_for(self.config, self.database, self.adopted)
        self.package_doc["configuration"] = deepcopy(self.target)
        self.package_doc["inventory_state_sha256"] = self.inventory["state_sha256"]
        self.package_json = json.dumps(self.package_doc, ensure_ascii=False, indent=2)
        self.package_hash = sha256_text(self.package_json)
        return self.status(token, installation, attempt, "WAITING")

    def status(self, token, installation, attempt, state="READY"):
        return dict(attempt_id=attempt, state=state, inventory_sha256=self.inventory_hash,
                    expires_at=(datetime.now(timezone.utc) + timedelta(seconds=120)).isoformat(),
                    retry_after_seconds=2, package_sha256=self.package_hash if state in {"READY", "APPLIED"} else None)

    def package(self, token, installation, attempt):
        result = dict(installation_id=installation, attempt_id=attempt, inventory_sha256=self.inventory_hash,
                      package_sha256=self.package_hash, package_json=self.package_json)
        if self.corruption:
            result[self.corruption] += " "
        return result

    def cancel(self, *args):
        self.calls.append(("cancel", args))
        if self.offline:
            raise MiddlewareError("OFFLINE", "offline", True)
        return {"state": "CANCELLED"}

    def ack(self, token, installation, attempt, digest, receipt):
        self.calls.append(("ack", token, receipt))
        if self.fail_ack:
            raise MiddlewareError("OFFLINE", "offline", True)
        assert digest == self.package_hash
        return self.status(token, installation, attempt, "APPLIED")


@pytest.fixture
def rollout(tmp_path):
    config, database, _, document = installed(tmp_path)
    document["api"]["use_ssl"] = True
    config.write_text(yaml.safe_dump(document), encoding="utf-8")
    adopted = upgrade_installation(config, tmp_path)
    server, graph = Server(config, database, adopted), Graph()
    coordinator = RolloutCoordinator(config, tmp_path, server, "legacy-access", graph)
    coordinator.discover()
    token = jwt.encode(dict(iss="https://native-auth.invalid/", aud="native-api", azp="native-client",
                            sub="test-subject", exp=int(time.time()) + 3600, scope="openid"),
                       "test-only-signing-key", algorithm="HS256")
    bundle = dict(access_token=token, refresh_token="independent-refresh")
    coordinator.prepare(bundle)
    return coordinator, server, graph, bundle, database


def test_full_real_inventory_consumer_and_native_token_survive(rollout):
    coordinator, server, graph, bundle, legacy = rollout
    coordinator.begin()
    assert graph.paused
    assert server.inventory["orders"][0]["legacy_delivery_id"] == "old-delivery"
    result = coordinator.poll()
    assert result.native_applied
    assert coordinator.state["state"] == "APPLIED"
    assert graph.resumed == 0
    assert rows(legacy, "SELECT external_delivery_id FROM DeliveryMapping") == [("old-delivery",)]
    assert read_token_from_file(result.settings.for_backend("native").token_path)["access_token"] == bundle["access_token"]
    assert not coordinator.prepared_path.exists()
    assert recover_local_rollout(coordinator.config_path, coordinator.bundle_dir)["state"] == "APPLIED"


@pytest.mark.parametrize("field", ["attempt_id", "installation_id", "inventory_sha256", "package_sha256", "package_json"])
def test_exact_envelope_and_utf8_string_digest_rejected(rollout, field):
    coordinator, server, graph, _, _ = rollout
    raw = coordinator.config_path.read_bytes()
    coordinator.begin()
    server.corruption = field
    with pytest.raises(MiddlewareError):
        coordinator.poll()
    assert coordinator.config_path.read_bytes() == raw
    assert read_state(coordinator.config_path)["state"] == "INVALIDATED"
    assert graph.resumed == 1


def test_changed_native_target_rejected_before_pause(rollout):
    coordinator, server, graph, _, _ = rollout
    server.target = deepcopy(server.target)
    server.target["api"]["native"]["connection_id"] = "10000000-0000-0000-0000-000000000099"
    with pytest.raises(MiddlewareError, match="changed"):
        coordinator.begin()
    assert not graph.paused


def test_timeout_offline_invalidation_precedes_resume(rollout):
    coordinator, server, graph, _, _ = rollout
    now = [1.]
    coordinator.clock = lambda: now[0]
    coordinator.begin(15)
    server.offline = True
    now[0] = 17.
    with pytest.raises(MiddlewareError, match="expired"):
        coordinator.poll()
    assert graph.resumed == 1
    state = read_state(coordinator.config_path)
    assert state["state"] == "INVALIDATED" and state["cancel_pending"]
    assert state["cancel_reason"] == "timeout"
    with pytest.raises(MiddlewareError):
        coordinator.apply()


def test_restart_quarantines_uncommitted_package_generic_startup_cannot_apply(rollout):
    coordinator, server, _, _, _ = rollout
    coordinator.begin()
    coordinator.package_path.write_text(server.package_json, encoding="utf-8")
    result = upgrade_installation(coordinator.config_path, coordinator.bundle_dir)
    assert not result.native_applied
    assert not coordinator.package_path.exists()
    assert read_state(coordinator.config_path)["state"] == "INVALIDATED"
    assert coordinator.package_path.with_name(coordinator.package_path.name + ".invalidated").exists()


def test_ack_loss_restart_uses_actual_receipt_without_transfer(rollout):
    coordinator, server, graph, bundle, _ = rollout
    server.fail_ack = True
    coordinator.begin()
    result = coordinator.poll()
    assert coordinator.state["state"] == "ACK_PENDING"
    raw = coordinator.config_path.read_bytes()
    restarted = RolloutCoordinator(coordinator.config_path, coordinator.bundle_dir, server, "legacy-access", graph)
    assert restarted.state["state"] == "ACK_PENDING"
    assert read_token_from_file(result.settings.token_path)["refresh_token"] == bundle["refresh_token"]
    server.fail_ack = False
    restarted.acknowledge(bundle["access_token"])
    assert coordinator.config_path.read_bytes() == raw
    assert graph.resumed == 0


def test_token_persistence_failure_prevents_config_commit(rollout, monkeypatch):
    coordinator, _, graph, _, _ = rollout
    coordinator.begin()
    raw = coordinator.config_path.read_bytes()
    def fail(*args):
        raise OSError("disk full")
    monkeypatch.setattr("utils.token_storage.store_token_at_file", fail)
    with pytest.raises(MiddlewareError, match="disk full"):
        coordinator.poll()
    assert coordinator.config_path.read_bytes() == raw
    assert graph.resumed == 1


def test_stale_inventory_never_relaxes_existing_consumer(rollout):
    coordinator, _, graph, _, database = rollout
    coordinator.begin()
    with sqlite3.connect(str(database)) as conn:
        conn.execute("UPDATE SyncOrders SET attempts=attempts+1")
    with pytest.raises(MiddlewareError, match="changed after inventory"):
        coordinator.poll()
    assert graph.resumed == 1


def test_pending_erp_effect_never_uploads_inventory(rollout):
    coordinator, server, graph, _, database = rollout
    with sqlite3.connect(str(database)) as conn:
        scope = conn.execute("SELECT scope FROM SyncScope").fetchone()[0]
        conn.execute("INSERT INTO SyncEffects(scope,internal_id,effect_key,operation,payload) VALUES(?,'123','pending','DEPARTURE','{}')", (scope,))
    with pytest.raises(MiddlewareError, match="ERP"):
        coordinator.begin()
    assert not hasattr(server, "inventory")
    assert graph.resumed == 1


def test_legacy_token_clone_rejected_even_same_auth(rollout):
    coordinator, _, _, bundle, _ = rollout
    store_token_at_file(bundle, coordinator.bundle_dir / "resources" / "token.txt")
    with pytest.raises(MiddlewareError, match="separate native"):
        coordinator.prepare(bundle)


def test_forged_metadata_does_not_make_wrong_token_eligible(rollout):
    coordinator, server, _, bundle, _ = rollout
    wrong = dict(bundle, middleware_auth=server.target["auth"])
    wrong["access_token"] = jwt.encode(dict(iss="https://wrong.invalid/", aud="native-api",
                                            azp="native-client"), "test", algorithm="HS256")
    with pytest.raises(MiddlewareError, match="authentication configuration"):
        coordinator.prepare(wrong)


def test_new_native_grant_without_scope_evidence_is_rejected(rollout):
    coordinator, server, _, bundle, _ = rollout
    claims = jwt.decode(bundle["access_token"], options={"verify_signature": False})
    claims.pop("scope")
    bundle["access_token"] = jwt.encode(claims, "test-key", algorithm="HS256")
    bundle["middleware_auth"] = server.target["auth"]
    with pytest.raises(MiddlewareError, match="authentication configuration"):
        coordinator.prepare(bundle)


def test_missing_committed_database_blocks_legacy_restart(rollout):
    coordinator, _, _, _, _ = rollout
    coordinator.begin()
    result = coordinator.poll()
    from pathlib import Path
    Path(result.database_path).unlink()
    with pytest.raises(sqlite3.OperationalError):
        recover_local_rollout(coordinator.config_path, coordinator.bundle_dir)


def test_false_quiescence_never_exports_or_uploads(rollout):
    coordinator, server, graph, _, _ = rollout
    graph.quiesce = lambda deadline: False
    with pytest.raises(MiddlewareError, match="drain"):
        coordinator.begin()
    assert not hasattr(server, "inventory")
    assert graph.resumed == 1


def test_failed_durable_invalidation_blocks_instead_of_resuming(rollout, monkeypatch):
    coordinator, _, graph, _, _ = rollout
    coordinator.begin()
    def fail(*args):
        raise OSError("disk full")
    monkeypatch.setattr("middleware_rollout.atomic_json", fail)
    with pytest.raises(OSError):
        coordinator.cancel()
    assert graph.paused and graph.resumed == 0
    assert coordinator.state["state"] == "WAITING"
    assert read_state(coordinator.config_path)["state"] == "WAITING"


def test_crash_after_config_commit_recovers_from_consumer_receipt(rollout, monkeypatch):
    coordinator, _, graph, _, _ = rollout
    coordinator.begin()
    import middleware_rollout
    real = middleware_rollout.atomic_json
    def fail_ack(path, state):
        if state["state"] == "ACK_PENDING":
            raise OSError("post-commit disk failure")
        return real(path, state)
    monkeypatch.setattr(middleware_rollout, "atomic_json", fail_ack)
    with pytest.raises(OSError):
        coordinator.poll()
    assert graph.resumed == 0
    assert graph.released
    assert read_state(coordinator.config_path)["state"] == "APPLYING"
    monkeypatch.setattr(middleware_rollout, "atomic_json", real)
    assert recover_local_rollout(coordinator.config_path, coordinator.bundle_dir)["state"] == "ACK_PENDING"
