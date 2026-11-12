"""Disposable relevant-schema/trigger checks, NOT a full Farmax cashier simulation.

Trigger bodies follow retained delivery-db/trigger-procedure-sources.md. The
physical definitions preserve nullable/nonunique delivery/sale identity and the
composite customer key. Only this fixture creates or changes ERP test data.
"""
import os
from pathlib import Path
from datetime import time

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL
from connectors.farmax.farmax_repository import FarmaxRepository
from connectors.farmax.farmax_setup import FarmaxSetup


@pytest.fixture
def firebird(tmp_path):
    if os.environ.get("VELIDE_TEST_FIREBIRD") != "1":
        pytest.skip("Set VELIDE_TEST_FIREBIRD=1 for a disposable localhost database")
    import fdb
    password = os.environ.get("VELIDE_TEST_FIREBIRD_PASSWORD", "masterkey")
    connection = fdb.create_database(dsn="localhost:" + str(tmp_path / "observer.fdb"), user="SYSDBA", password=password, charset="UTF8")
    engine = None
    try:
        cursor = connection.cursor()
        schema = (Path(__file__).parents[1] / "fixtures" / "farmax_observer.sql").read_text(encoding="utf-8")
        statements = [item for item in schema.split(";") if item.strip()]
        for statement in statements:
            # Commit DDL before preparing fixture data against it.
            if statement.lstrip().upper().startswith("INSERT"):
                connection.commit()
            cursor.execute(statement)
        connection.commit()
        for statement in [
            "CREATE SEQUENCE DELIVERYLOG_ID_AUTOINCREMENT",
            "CREATE TRIGGER TG_AT_ENVIADO_ENTREGAS FOR ENTREGAS ACTIVE BEFORE INSERT OR UPDATE POSITION 0 AS BEGIN NEW.ENVIADO=' '; END",
            "CREATE TRIGGER VENDAS_BIU0 FOR VENDAS ACTIVE BEFORE INSERT OR UPDATE POSITION 0 AS BEGIN NEW.CHEQUE=''; END",
            "CREATE TRIGGER TRG_DELIVERY_LOGID_INCREMENT FOR DELIVERYLOG ACTIVE BEFORE INSERT POSITION 0 AS BEGIN NEW.ID=NEXT VALUE FOR DELIVERYLOG_ID_AUTOINCREMENT; END",
            """CREATE TRIGGER TRG_ADD_DELIVERY FOR ENTREGAS ACTIVE AFTER INSERT OR UPDATE OR DELETE POSITION 0 AS BEGIN
                IF (INSERTING) THEN INSERT INTO DELIVERYLOG(CD_VENDA,ACTION,LOGDATE) VALUES(NEW.CD_VENDA,'INSERT',CURRENT_TIMESTAMP);
                ELSE IF (UPDATING) THEN INSERT INTO DELIVERYLOG(CD_VENDA,ACTION,LOGDATE) VALUES(NEW.CD_VENDA,'UPDATE',CURRENT_TIMESTAMP);
                ELSE IF (DELETING) THEN INSERT INTO DELIVERYLOG(CD_VENDA,ACTION,LOGDATE) VALUES(OLD.CD_VENDA,'DELETE',CURRENT_TIMESTAMP);
            END""",
        ]:
            cursor.execute(statement)
            connection.commit()
        engine = create_engine(URL.create("firebird+fdb", username="SYSDBA", password=password, host="localhost", database=str(tmp_path / "observer.fdb")))
        yield FarmaxRepository(engine), engine
    finally:
        if engine:
            engine.dispose()
        connection.drop_database()


def test_observation_fires_no_delivery_or_sale_triggers(firebird):
    repo, engine = firebird
    report = FarmaxSetup(engine).initial_setup()
    assert report["observe_only"] and report["capture_matches_retained"]
    original = repo.fetch_deliveries_by_id((123,), pending_only=False)[0]
    payload = dict(source_context=original.source_context, operation="IN_ROUTE", courier_local_id="5",
                   started_at="2026-11-11T15:00:00Z", completed_at=None)
    assert repo.observe_delivery_effect(123,payload,"America/Sao_Paulo") == "DEPARTURE_FIELDS_MATCH"
    with pytest.raises(ValueError, match="disabled"):
        repo.update_delivery_as_done(123,time(13))
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM DELIVERYLOG")).scalar() == 0
        assert conn.execute(text("SELECT ENVIADO,ID_OPERADOR_CAIXA_ORIGINAL FROM ENTREGAS")).fetchone() == ("S",90)
        assert conn.execute(text("SELECT CHEQUE,CONCLUIDO,HORAFINAL FROM VENDAS WHERE ID_PRODUTO=101")).fetchone() == ("retained","S",time(11,5))
        assert conn.execute(text("SELECT ID_OPERADORCAIXA,VALOR FROM CAIXA")).fetchone() == (90,30)
    # Simulate the native operator recording arrival. Its real trigger effects
    # occur here, not during recognition/replay by the integration.
    with engine.begin() as conn:
        conn.execute(text("UPDATE ENTREGAS SET STATUS='V',HORA_CHEGADA='13:00:00'"))
    payload["completed_at"] = "2026-11-11T16:00:00Z"
    for _ in range(2):
        assert repo.observe_delivery_effect(123,payload,"America/Sao_Paulo") == "COMPLETED_FIELDS_MATCH"
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM DELIVERYLOG")).scalar() == 1
        assert conn.execute(text("SELECT CHEQUE FROM VENDAS WHERE ID_PRODUTO=101")).scalar() == "retained"


def test_unknown_active_capture_is_reported_and_never_rewritten(firebird):
    _, engine = firebird
    with engine.begin() as conn:
        conn.execute(text("CREATE OR ALTER TRIGGER TRG_ADD_DELIVERY FOR ENTREGAS ACTIVE AFTER INSERT POSITION 0 AS BEGIN INSERT INTO DELIVERYLOG(CD_VENDA,ACTION,LOGDATE) VALUES(NEW.CD_VENDA,'INSERT',CURRENT_TIMESTAMP); END"))
    assert FarmaxSetup(engine).initial_setup()["capture_matches_retained"] is False
    with engine.connect() as conn:
        assert conn.execute(text("SELECT RDB$TRIGGER_TYPE FROM RDB$TRIGGERS WHERE RDB$TRIGGER_NAME='TRG_ADD_DELIVERY'")).scalar() == 2
