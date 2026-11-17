from concurrent.futures import ThreadPoolExecutor
import sqlite3

import pytest

from api.backend_ownership import BackendOwnership, ProfileOwnership, OwnershipError
from api.sync_journal import SyncJournal


def ledger(tmp_path):
    registry = BackendOwnership(tmp_path / "owners.db", "source")
    registry.register("legacy", "https://legacy.invalid|CDS")
    registry.register("native", "https://native.invalid/graphql|CDS|merchant|connection")
    registry.select("legacy")
    return registry


def journal(tmp_path, registry, name):
    return SyncJournal(str(tmp_path / (name + ".db")), "https://" + name + ".invalid|CDS",
                       source_timezone="America/Sao_Paulo", ownership=ProfileOwnership(registry, name))


def payload(internal="123"):
    return {"internal_id": internal, "customerName": "Cliente", "address": "Rua A, 1",
            "createdAt": "2026-11-13T15:00:00+00:00"}


def test_legacy_native_legacy_selects_only_new_orders(tmp_path):
    registry = ledger(tmp_path)
    legacy, native = [journal(tmp_path, registry, name) for name in ("legacy", "native")]
    legacy.accept("123", payload())
    registry.select("native")
    native.accept("123", payload())
    native.accept("456", payload("456"))
    registry.select("legacy")
    legacy.accept("456", payload("456"))
    legacy.accept("789", payload("789"))
    assert {value: registry.owner(value) for value in ("123", "456", "789")} == {"123": "legacy", "456": "native", "789": "legacy"}
    assert [row["internal_id"] for row in legacy.restore()] == ["123", "789"]
    assert [row["internal_id"] for row in native.restore()] == ["456"]
    assert legacy.claim()["internal_id"] == "123"
    assert native.claim(native=True)["internal_id"] == "456"


def test_first_foreign_scan_persists_receipt_for_logged_out_owner(tmp_path):
    registry = ledger(tmp_path)
    legacy, native = [journal(tmp_path, registry, name) for name in ("legacy", "native")]
    registry.select("native")
    assert legacy.accept("123", payload())["skipped_backend"]
    assert legacy.claim() is None
    # Reopen both SQLite files: there was no owner-journal commit before restart.
    recovered = journal(tmp_path, registry, "native")
    assert recovered.claim(native=True)["internal_id"] == "123"
    assert recovered.get_order("123")["payload"]


def test_concurrent_scans_and_selection_never_create_two_owners(tmp_path):
    registry = ledger(tmp_path)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: registry.intake("123", payload()), range(30)))
    registry.select("native")
    assert registry.intake("123", payload()) == "legacy"
    with sqlite3.connect(registry.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM Owners").fetchone()[0] == 1


def test_failed_config_publication_does_not_change_intake(tmp_path):
    registry = ledger(tmp_path)
    def fail():
        raise OSError("disk full")
    with pytest.raises(OSError):
        registry.select("native", publish=fail)
    assert registry.selected() == "legacy"


def test_profile_source_and_scope_cannot_be_retargeted(tmp_path):
    registry = ledger(tmp_path)
    with pytest.raises(OwnershipError):
        BackendOwnership(registry.path, "other source")
    with pytest.raises(OwnershipError):
        registry.register("legacy", "https://another-store.invalid|CDS")


def test_legacy_principal_binding_survives_restart_and_new_cache(tmp_path):
    registry = ledger(tmp_path)
    registry.seed("legacy", ["123"], "issuer/account-A")
    registry.seed("legacy", ["123"], "issuer/account-B")
    with pytest.raises(OwnershipError, match="conta original"):
        registry.bind_principal("legacy", "issuer/account-B")
    registry.bind_principal("legacy", "issuer/account-A")


def test_unknown_historical_principal_is_not_adopted_from_later_login(tmp_path):
    registry = ledger(tmp_path)
    registry.seed("legacy", ["123"])
    registry.seed("legacy", ["123"], "new account")
    with pytest.raises(OwnershipError):
        registry.bind_principal("legacy", "new account")


def test_empty_installation_can_login_after_offline_intake_and_restart(tmp_path):
    registry = ledger(tmp_path)
    registry.seed("legacy", [])
    legacy = journal(tmp_path, registry, "legacy")
    legacy.accept("123", payload())
    registry.seed("legacy", ["123"])
    registry.bind_principal("legacy", "first verified account")


def test_late_source_conflict_cannot_be_acknowledged_by_old_journal_commit(tmp_path):
    registry = ledger(tmp_path)
    legacy = journal(tmp_path, registry, "legacy")
    legacy.accept("123", payload())
    registry.intake("123", dict(payload(), address="Changed"))
    registry.recorded("legacy", "123")  # Delayed completion of original accept.
    assert registry.receipts("legacy")[0]["conflict"]
    assert legacy.claim() is None
    assert legacy.get_order("123")["source_review"]
    assert registry.receipts("legacy") == []


def test_native_principal_can_change_after_server_scope_verification(tmp_path):
    registry = ledger(tmp_path)
    registry.bind_principal("native", "old-tenant/user")
    registry.bind_principal("native", "new-tenant/user")


def test_source_conflict_in_foreign_scanner_blocks_the_owner_before_dispatch(tmp_path):
    registry = ledger(tmp_path)
    legacy, native = [journal(tmp_path, registry, name) for name in ("legacy", "native")]
    legacy.accept("123", payload())
    native.accept("123", dict(payload(), address="Alterado"))
    assert not registry.can_execute("legacy", "123")
    assert legacy.claim() is None
    assert legacy.get_order("123")["source_review"]


def test_authoritative_offline_transfer_fences_original_journal(tmp_path):
    registry = ledger(tmp_path)
    legacy = journal(tmp_path, registry, "legacy")
    legacy.accept("123", payload())
    registry.transfer(["123"], "legacy", "native")
    assert not legacy.restore()
    assert legacy.claim() is None
    with pytest.raises(ValueError, match="proprietária"):
        legacy.cancel("123", "old-remote")


def test_native_bootstrap_cannot_reclaim_legacy_owned_order(tmp_path):
    registry = ledger(tmp_path)
    registry.seed("legacy", ["123"], "original")
    native = journal(tmp_path, registry, "native")
    native.native_states([{"externalOrderId": "123", "found": True, "deliveryId": "remote",
                          "deleted": False, "cancellation": "NONE", "revision": "1"}])
    assert not native.restore()
    assert registry.owner("123") == "legacy"


def test_frozen_request_stays_in_original_journal_after_selection(tmp_path):
    registry = ledger(tmp_path)
    legacy = journal(tmp_path, registry, "legacy")
    legacy.accept("123", payload())
    legacy.freeze_request("123", {"idempotencyKey": "original", "timestamp": 100, "offset": 0})
    first = legacy.claim()
    registry.select("native")
    assert legacy.dispatch_is_current("123", first["attempts"], "ADD")
    assert journal(tmp_path, registry, "native").claim(native=True) is None
    assert "original" in legacy.claim()["request"]
