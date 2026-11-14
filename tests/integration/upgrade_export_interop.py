"""Cross-language test bridge. Only operates in the explicitly supplied test folder."""
import json
import os
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[2]
os.environ["VELIDE_CONFIG_PATH"] = str(ROOT / "tests" / "fixtures" / "test_config.yml")
sys.path.insert(0, str(ROOT / "src"))

from local_upgrade import upgrade_installation
from upgrade_inventory import export_upgrade_inventory
from test_automatic_upgrade import installed, receipt, rows
from api.sync_journal import SyncJournal
from utils.source_identity import configured_scope


def main():
    mode, directory = sys.argv[1:]
    root = Path(directory).resolve()
    assert root.is_dir()
    config = root / "resources" / "config.yml"
    database = root / "resources" / "velide.db"
    if mode == "create":
        _, _, folder, _ = installed(root, "CDS")
        receipt(folder, "ent-processed.json")
        receipt(folder, "ent-unknown.json")
        receipt(folder, "ent-linked.json")
        (folder / "ent-incomplete.json").write_bytes(b'{"horario_pedido":')
        with sqlite3.connect(str(database)) as conn:
            conn.execute("INSERT INTO DeliveryMapping(external_delivery_id,internal_delivery_id,status) VALUES('old-history','historical','ENTREGUE')")
            conn.execute("INSERT INTO DeliveryMapping(external_delivery_id,internal_delivery_id,status) VALUES('old-cancelled','cancelled','CANCELADA')")
        adopted = upgrade_installation(config, root)
        journal = SyncJournal(str(database), configured_scope(adopted.settings))
        journal.accept("uncertain", {"internal_id": "uncertain", "source_context": {"branch_id": "7"}})
        journal.freeze_request("uncertain", {"idempotencyKey": "original-key"})
        journal.claim()
        journal.accept("accepted", {"internal_id": "accepted", "customerName": "Accepted job evidence"})
        inventory = export_upgrade_inventory(config, root)
        (root / "inventory.json").write_text(json.dumps(inventory), encoding="utf-8")
        print(json.dumps({"orders": len(inventory["orders"])}))
    elif mode == "apply":
        before = rows(database, "SELECT * FROM DeliveryMapping")
        result = upgrade_installation(config, root)
        assert result.native_applied, result.package_error
        assert result.settings.api.use_neighbourhood
        assert result.settings.api.add_delivery_max_retries == 7
        states = dict(rows(result.database_path, "SELECT internal_id,submission FROM SyncOrders"))
        assert states["historical"] == "TERMINAL" and states["uncertain"] == "NEEDS_REVIEW"
        assert states["cancelled"] == "TERMINAL" and states["accepted"] == "ACCEPTED"
        assert SyncJournal(result.database_path, configured_scope(result.settings)).claim(native=True) is None
        assert rows(result.database_path, "SELECT job_id FROM SyncOrders WHERE internal_id='accepted'") == [("10000000-0000-0000-0000-000000000005",)]
        inventory = json.loads((root / "inventory.json").read_text(encoding="utf-8"))
        for entry in inventory["cds_files"]:
            if entry["filename"] == "ent-processed.json":
                assert states[entry["internal_id"]] == "TERMINAL"
            if entry["filename"] == "ent-unknown.json":
                assert states[entry["internal_id"]] == "NEEDS_REVIEW"
            if entry["filename"] == "ent-linked.json":
                assert states[entry["internal_id"]] == "IMPORTED"
                assert rows(result.database_path, "SELECT external_delivery_id FROM DeliveryMapping WHERE internal_delivery_id=?",
                            (entry["internal_id"],)) == [("10000000-0000-0000-0000-000000000007",)]
        repeated = upgrade_installation(config, root)
        assert not repeated.package_error and not repeated.native_applied
        assert rows(database, "SELECT * FROM DeliveryMapping") == before
        print(json.dumps({"native_applied": True, "unchanged_legacy_database": True, "repeatable": True}))
    else:
        raise ValueError("Unknown test bridge operation")


if __name__ == "__main__":
    main()
