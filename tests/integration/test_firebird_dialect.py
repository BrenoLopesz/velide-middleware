"""Disposable relevant-schema/trigger checks, NOT a full Farmax cashier simulation.

Trigger bodies follow retained delivery-db/trigger-procedure-sources.md. The
physical definitions preserve nullable/nonunique delivery/sale identity and the
composite customer key. Only this fixture creates or changes ERP test data.
"""
import os
from pathlib import Path
from datetime import time

import pytest
from sqlalchemy import create_engine, text, event
from sqlalchemy.exc import DBAPIError
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
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM DELIVERYLOG")).scalar() == 0
        assert conn.execute(text("SELECT ENVIADO,ID_OPERADOR_CAIXA_ORIGINAL FROM ENTREGAS")).fetchone() == ("S",90)
        assert conn.execute(text("SELECT CHEQUE,CONCLUIDO,HORAFINAL FROM VENDAS WHERE ID_PRODUTO=101")).fetchone() == ("retained","S",time(11,5))
        assert conn.execute(text("SELECT ID_OPERADORCAIXA,VALOR FROM CAIXA")).fetchone() == (90,30)
    # Simulate the native operator recording arrival. Its real trigger effects
    # occur here, not during recognition/replay by the integration.
    with engine.begin() as conn:
        conn.execute(text("UPDATE ENTREGAS SET STATUS='V',HORA_CHEGADA='13:00:00'"))
        conn.execute(text("UPDATE VENDAS SET STATUS='V'"))
    payload["completed_at"] = "2026-11-11T16:00:00Z"
    for _ in range(2):
        assert repo.observe_delivery_effect(123,payload,"America/Sao_Paulo") == "COMPLETED_FIELDS_MATCH"
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM DELIVERYLOG")).scalar() == 1
        assert conn.execute(text("SELECT CHEQUE FROM VENDAS WHERE ID_PRODUTO=101")).scalar() == ""


def test_unknown_active_capture_is_reported_and_never_rewritten(firebird):
    _, engine = firebird
    with engine.begin() as conn:
        conn.execute(text("CREATE OR ALTER TRIGGER TRG_ADD_DELIVERY FOR ENTREGAS ACTIVE AFTER INSERT POSITION 0 AS BEGIN INSERT INTO DELIVERYLOG(CD_VENDA,ACTION,LOGDATE) VALUES(NEW.CD_VENDA,'INSERT',CURRENT_TIMESTAMP); END"))
    assert FarmaxSetup(engine).initial_setup()["capture_matches_retained"] is False
    with engine.connect() as conn:
        assert conn.execute(text("SELECT RDB$TRIGGER_TYPE FROM RDB$TRIGGERS WHERE RDB$TRIGGER_NAME='TRG_ADD_DELIVERY'")).scalar() == 2


def execution(repo, operation="DONE", **changes):
    value = dict(source_context=repo.fetch_deliveries_by_id((123,), pending_only=False)[0].source_context,
                 operation=operation, courier_local_id="5", started_at="2026-11-11T15:00:00Z",
                 completed_at="2026-11-11T16:00:00Z" if operation == "DONE" else None)
    value.update(changes)
    return value


def test_real_writes_trigger_once_and_replay_without_more_dml(firebird):
    repo, engine = firebird
    with engine.begin() as conn:
        conn.execute(text("UPDATE ENTREGAS SET STATUS='S',HORA_SAIDA=NULL,CD_ENTREGADOR=NULL"))
        conn.execute(text("DELETE FROM DELIVERYLOG"))
    departure = execution(repo, "IN_ROUTE")
    assert repo.apply_delivery_effect(123, departure, "America/Sao_Paulo") == "DEPARTURE_FIELDS_MATCH"
    assert repo.apply_delivery_effect(123, departure, "America/Sao_Paulo") == "DEPARTURE_FIELDS_MATCH"
    done = execution(repo)
    assert repo.apply_delivery_effect(123, done, "America/Sao_Paulo") == "COMPLETED_FIELDS_MATCH"
    assert repo.apply_delivery_effect(123, done, "America/Sao_Paulo") == "COMPLETED_FIELDS_MATCH"
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM DELIVERYLOG")).scalar() == 2
        assert conn.execute(text("SELECT STATUS,HORA_SAIDA,HORA_CHEGADA,ENVIADO,ID_OPERADOR_CAIXA_ORIGINAL FROM ENTREGAS")).fetchone() == (
            "V", time(12), time(13), " ", 90)
        assert conn.execute(text("SELECT STATUS,CHEQUE,CONCLUIDO,HORAFINAL FROM VENDAS WHERE ID_PRODUTO=101")).fetchone() == (
            "V", "", "S", time(11, 5))
        assert conn.execute(text("SELECT ID_OPERADORCAIXA,VALOR FROM CAIXA")).fetchone() == (90, 30)


def test_firebird_overnight_precision_and_partial_cancellation(firebird):
    repo, engine = firebird
    with engine.begin() as conn:
        conn.execute(text("UPDATE ENTREGAS SET STATUS='S',HORA_SAIDA=NULL,DATA='2026-11-12'"))
        conn.execute(text("UPDATE VENDAS SET STATUS='C' WHERE ID_PRODUTO=101"))
        conn.execute(text("UPDATE VENDAS SET CONCLUIDO='N' WHERE ID_PRODUTO=102"))
    request = execution(repo, started_at="2026-11-13T02:55:00.123456Z", completed_at="2026-11-13T03:10:00.654321Z")
    for _ in range(2):
        assert repo.apply_delivery_effect(123, request, "America/Sao_Paulo") == "COMPLETED_FIELDS_MATCH"
    with engine.connect() as conn:
        assert conn.execute(text("SELECT HORA_SAIDA,HORA_CHEGADA FROM ENTREGAS")).fetchone() == (time(23,55,0,123400), time(0,10,0,654300))
        assert conn.execute(text("SELECT STATUS,CONCLUIDO,HORAFINAL FROM VENDAS ORDER BY ORDEM")).fetchall() == [
            ("C", "S", time(11,5)), ("V", "N", time(11,5))]


def test_firebird_completion_preserves_native_arrival(firebird):
    repo, engine = firebird
    with engine.begin() as conn:
        conn.execute(text("UPDATE ENTREGAS SET HORA_CHEGADA='12:50:00'"))
    assert repo.apply_delivery_effect(123, execution(repo), "America/Sao_Paulo") == "COMPLETED_FIELDS_MATCH"
    with engine.connect() as conn:
        assert conn.execute(text("SELECT HORA_CHEGADA FROM ENTREGAS")).scalar() == time(12,50)


def test_firebird_later_item_failure_rolls_back_delivery_logs_and_items(firebird):
    repo, engine = firebird
    with engine.begin() as conn:
        conn.execute(text("CREATE EXCEPTION FAIL_ITEM 'Injected item failure'"))
    with engine.begin() as conn:
        conn.execute(text("CREATE TRIGGER FAIL_SECOND FOR VENDAS ACTIVE BEFORE UPDATE POSITION 1 AS BEGIN IF (OLD.ID_PRODUTO=102) THEN EXCEPTION FAIL_ITEM; END"))
    with pytest.raises(DBAPIError):
        repo.apply_delivery_effect(123, execution(repo), "America/Sao_Paulo")
    with engine.connect() as conn:
        assert conn.execute(text("SELECT STATUS,HORA_CHEGADA FROM ENTREGAS")).fetchone() == ("R", None)
        assert conn.execute(text("SELECT COUNT(*) FROM DELIVERYLOG")).scalar() == 0
        assert conn.execute(text("SELECT STATUS,CHEQUE FROM VENDAS ORDER BY ORDEM")).fetchall() == [("S", "retained"), ("S", "retained")]


def test_firebird_capture_install_on_new_database_preserves_native_triggers(firebird):
    repo, engine = firebird
    for statement in ("DROP TRIGGER TRG_ADD_DELIVERY", "DROP TRIGGER TRG_DELIVERY_LOGID_INCREMENT",
                      "DROP SEQUENCE DELIVERYLOG_ID_AUTOINCREMENT", "DROP TABLE DELIVERYLOG"):
        with engine.begin() as conn:
            conn.execute(text(statement))
    with engine.begin() as conn:
        conn.execute(text("CREATE TRIGGER LOCAL_CUSTOM FOR ENTREGAS ACTIVE BEFORE UPDATE POSITION 5 AS BEGIN NEW.NOME=OLD.NOME; END"))
    setup = FarmaxSetup(engine)
    report = setup.initial_setup()
    assert report["capture_matches_retained"]
    assert set(report["created_objects"]) == {"DELIVERYLOG", "DELIVERYLOG_ID_AUTOINCREMENT", "TRG_ADD_DELIVERY", "TRG_DELIVERY_LOGID_INCREMENT"}
    statements = []
    event.listen(engine, "before_cursor_execute", lambda c, cur, sql, *args: statements.append(sql))
    assert setup.initial_setup()["created_objects"] == []
    assert all(sql.lstrip().upper().startswith("SELECT") for sql in statements)
    assert repo.apply_delivery_effect(123, execution(repo), "America/Sao_Paulo") == "COMPLETED_FIELDS_MATCH"
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM DELIVERYLOG")).scalar() == 1
        assert conn.execute(text("SELECT COUNT(*) FROM RDB$TRIGGERS WHERE RDB$TRIGGER_NAME IN ('LOCAL_CUSTOM','TG_AT_ENVIADO_ENTREGAS')")).scalar() == 2


def test_firebird_missing_sequence_seeded_above_retained_log_ids(firebird):
    _, engine = firebird
    for statement in ("DROP TRIGGER TRG_DELIVERY_LOGID_INCREMENT", "DROP SEQUENCE DELIVERYLOG_ID_AUTOINCREMENT"):
        with engine.begin() as conn:
            conn.execute(text(statement))
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO DELIVERYLOG(ID,CD_VENDA,ACTION) VALUES(1200,123,'UPDATE')"))
    assert FarmaxSetup(engine).initial_setup()["capture_matches_retained"]
    with engine.begin() as conn:
        conn.execute(text("UPDATE ENTREGAS SET STATUS='R'"))
    with engine.connect() as conn:
        assert conn.execute(text("SELECT MAX(ID) FROM DELIVERYLOG")).scalar() == 1201


def test_firebird_inactive_owned_trigger_not_reactivated(firebird):
    _, engine = firebird
    with engine.begin() as conn:
        conn.execute(text("ALTER TRIGGER TRG_ADD_DELIVERY INACTIVE"))
    report = FarmaxSetup(engine).initial_setup()
    assert report["conflicts"] and not report["capture_matches_retained"]
    assert report["created_objects"] == []
    with engine.connect() as conn:
        assert conn.execute(text("SELECT RDB$TRIGGER_INACTIVE FROM RDB$TRIGGERS WHERE RDB$TRIGGER_NAME='TRG_ADD_DELIVERY'")).scalar() == 1


def test_firebird_collision_on_unrelated_relation_is_preserved(firebird):
    _, engine = firebird
    with engine.begin() as conn:
        conn.execute(text("DROP TRIGGER TRG_ADD_DELIVERY"))
    with engine.begin() as conn:
        conn.execute(text("CREATE TRIGGER TRG_ADD_DELIVERY FOR CAIXA ACTIVE BEFORE UPDATE POSITION 0 AS BEGIN NEW.VALOR=OLD.VALOR; END"))
    report = FarmaxSetup(engine).initial_setup()
    assert report["conflicts"] and report["created_objects"] == []
    with engine.connect() as conn:
        assert conn.execute(text("SELECT RDB$RELATION_NAME FROM RDB$TRIGGERS WHERE RDB$TRIGGER_NAME='TRG_ADD_DELIVERY'")).scalar().strip() == "CAIXA"


def test_firebird_custom_trigger_cannot_produce_false_success(firebird):
    repo, engine = firebird
    with engine.begin() as conn:
        conn.execute(text("CREATE TRIGGER CHANGE_RETURN FOR ENTREGAS ACTIVE BEFORE UPDATE POSITION 5 AS BEGIN NEW.STATUS='R'; END"))
    with pytest.raises(ValueError, match="Persisted delivery projection"):
        repo.apply_delivery_effect(123, execution(repo), "America/Sao_Paulo")
    with engine.connect() as conn:
        assert conn.execute(text("SELECT STATUS,HORA_CHEGADA FROM ENTREGAS")).fetchone() == ("R", None)
        assert conn.execute(text("SELECT COUNT(*) FROM DELIVERYLOG")).scalar() == 0


def test_firebird_old_context_and_velide_correction_of_manual_departure(firebird):
    repo, engine = firebird
    request = execution(repo)
    del request["source_context"]["sale_id"]
    with engine.begin() as conn:
        conn.execute(text("UPDATE ENTREGAS SET CD_ENTREGADOR=99,HORA_SAIDA='11:40:00',HORA_CHEGADA='12:55:00'"))
        conn.execute(text("UPDATE VENDAS SET STATUS='C' WHERE ID_PRODUTO=101"))
        conn.execute(text("UPDATE VENDAS SET TIPO_VENDA='E',PRECO_TOTAL=0,CONCLUIDO='N' WHERE ID_PRODUTO=102"))
        conn.execute(text("DELETE FROM DELIVERYLOG"))
    route = dict(request, operation="IN_ROUTE", completed_at=None)
    assert repo.apply_delivery_effect(123, route, "America/Sao_Paulo") == "DEPARTURE_FIELDS_MATCH"
    assert repo.apply_delivery_effect(123, request, "America/Sao_Paulo") == "COMPLETED_FIELDS_MATCH"
    assert repo.apply_delivery_effect(123, request, "America/Sao_Paulo") == "COMPLETED_FIELDS_MATCH"
    with engine.connect() as conn:
        assert conn.execute(text("SELECT STATUS,CD_ENTREGADOR,HORA_SAIDA,HORA_CHEGADA,ID_OPERADOR_CAIXA_ORIGINAL FROM ENTREGAS")).fetchone() == (
            "V", 5, time(12), time(12,55), 90)
        assert conn.execute(text("SELECT STATUS,CONCLUIDO FROM VENDAS ORDER BY ORDEM")).fetchall() == [("C", "S"), ("V", "N")]
        assert conn.execute(text("SELECT COUNT(*) FROM DELIVERYLOG")).scalar() == 2


def test_firebird_original_adoption_with_missing_checkout_metadata(firebird):
    repo, engine = firebird
    with engine.begin() as conn:
        conn.execute(text("UPDATE ENTREGAS SET STATUS='S',CD_ENTREGADOR=NULL,HORA_SAIDA=NULL"))
        conn.execute(text("UPDATE VENDAS SET TIPO_VENDA=NULL,PRECO_TOTAL=NULL,CONCLUIDO='N',ENTREGA=NULL"))
    assert len(repo.fetch_deliveries_by_id((123,))) == 1
    request = execution(repo, source_context=None, legacy_adopted=True)
    assert repo.apply_delivery_effect(123, request, "America/Sao_Paulo") == "COMPLETED_FIELDS_MATCH"
    with engine.connect() as conn:
        assert conn.execute(text("SELECT STATUS,CONCLUIDO,HORAFINAL FROM VENDAS ORDER BY ORDEM")).fetchall() == [
            ("V", "N", time(11,5)), ("V", "N", time(11,5))]
