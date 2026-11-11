"""Opt-in disposable database: never accepts an existing customer database path."""
import os
from datetime import time

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL

from connectors.farmax.farmax_repository import FarmaxRepository
from connectors.farmax.cancellation import classify_cancellation, CancellationDecision


@pytest.fixture
def firebird(tmp_path):
    if os.environ.get("VELIDE_TEST_FIREBIRD") != "1":
        pytest.skip("Set VELIDE_TEST_FIREBIRD=1 to create a disposable local Firebird database")
    import fdb
    path = str(tmp_path / "farmax-fixture.fdb")
    password = os.environ.get("VELIDE_TEST_FIREBIRD_PASSWORD", "masterkey")
    connection = fdb.create_database(dsn="localhost:" + path, user="SYSDBA", password=password, charset="UTF8")
    statements = [
        "CREATE TABLE ENTREGAS(CD_VENDA DOUBLE PRECISION,CD_ENTREGADOR DOUBLE PRECISION,CD_CLIENTE DOUBLE PRECISION,NOME VARCHAR(100),BAIRRO VARCHAR(100),DATA DATE,HORA_SAIDA TIME,HORA_CHEGADA TIME,STATUS CHAR(1))",
        "CREATE TABLE VENDAS(CD_VENDA DOUBLE PRECISION,CD_PRODUTO DOUBLE PRECISION,HORA TIME,TEMPENDERECO VARCHAR(200),TEMPREFERENCIA VARCHAR(100),STATUS CHAR(1),CONCLUIDO CHAR(1),HORAFINAL TIME)",
        "CREATE TABLE CLIENTES(CD_CLIENTE DOUBLE PRECISION,FONE VARCHAR(30))",
        "CREATE TABLE DELIVERYLOG(ID INTEGER,CD_VENDA DOUBLE PRECISION,ACTION VARCHAR(6),LOGDATE TIMESTAMP)",
        "CREATE SEQUENCE DELIVERYLOG_ID",
        "CREATE TABLE VENDEDORES(CD_VENDEDOR DOUBLE PRECISION, NOME VARCHAR(100), CD_FILIAL DOUBLE PRECISION, TIPO_FUNCIONARIO CHAR(1), STATUS CHAR(1))",
        "CREATE TABLE PARAMETROS(CD_FILIAL DOUBLE PRECISION)",
    ]
    cursor = connection.cursor()
    for statement in statements:
        cursor.execute(statement)
    connection.commit()
    cursor.execute("""CREATE TRIGGER TRG_ADD_DELIVERY FOR ENTREGAS ACTIVE AFTER INSERT OR UPDATE OR DELETE POSITION 0 AS
        BEGIN
          IF (INSERTING) THEN INSERT INTO DELIVERYLOG VALUES(NEXT VALUE FOR DELIVERYLOG_ID,NEW.CD_VENDA,'INSERT',CURRENT_TIMESTAMP);
          IF (UPDATING) THEN INSERT INTO DELIVERYLOG VALUES(NEXT VALUE FOR DELIVERYLOG_ID,NEW.CD_VENDA,'UPDATE',CURRENT_TIMESTAMP);
          IF (DELETING) THEN INSERT INTO DELIVERYLOG VALUES(NEXT VALUE FOR DELIVERYLOG_ID,OLD.CD_VENDA,'DELETE',CURRENT_TIMESTAMP);
        END""")
    connection.commit()
    engine = create_engine(URL.create("firebird+fdb", username="SYSDBA", password=password, host="localhost", database=path))
    try:
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO PARAMETROS VALUES(1)"))
            conn.execute(text("INSERT INTO VENDEDORES VALUES(5,'Courier',1,'E','A')"))
            conn.execute(text("INSERT INTO ENTREGAS(CD_VENDA,NOME,STATUS,DATA) VALUES(123,'Fixture','S','2026-11-11')"))
            conn.execute(text("INSERT INTO VENDAS(CD_VENDA,CD_PRODUTO,HORA,TEMPENDERECO,STATUS,CONCLUIDO) VALUES(123,1,'12:00','Rua A','V','N')"))
            conn.execute(text("INSERT INTO VENDAS(CD_VENDA,CD_PRODUTO,HORA,TEMPENDERECO,STATUS,CONCLUIDO) VALUES(123,2,'12:00','Rua A','D','S')"))
        yield FarmaxRepository(engine), engine
    finally:
        engine.dispose()
        connection.drop_database()


def test_firebird_lock_status_snapshot_and_replay(firebird):
    repo, engine = firebird
    from connectors.farmax.farmax_setup import FarmaxSetup
    setup = FarmaxSetup(engine)
    with engine.begin() as conn:
        setup._validate_existing_log(conn)
        assert setup._installed_trigger(conn, "TRG_ADD_DELIVERY", "ENTREGAS")
        setup._setup_delivery_log_trigger(conn)  # installed trigger is preserved
    assert repo.fetch_pending_ids() == [123]
    assert len(repo.fetch_deliveries_by_id((123,))) == 1
    assert classify_cancellation(repo.fetch_sales_statuses_by_id((123,))) == CancellationDecision.UNRESOLVED
    repo.update_delivery_as_in_route(123, 5, time(12, 30))
    repo.update_delivery_as_in_route(123, 5, time(12, 30))
    assert repo.fetch_deliveries_by_id((123,), pending_only=False)
    repo.update_delivery_as_done(123, time(13))
    repo.update_delivery_as_done(123, time(13))
    with engine.connect() as conn:
        assert [row[0] for row in conn.execute(text("SELECT STATUS FROM VENDAS ORDER BY CD_PRODUTO"))] == ["V", "D"]
        assert conn.execute(text("SELECT COUNT(*) FROM DELIVERYLOG")).scalar() == 3
    with pytest.raises(ValueError, match="conflicts"):
        repo.update_delivery_as_in_route(123, 5, time(12, 30))
    with engine.begin() as conn:
        conn.execute(text("UPDATE VENDAS SET STATUS='I',CONCLUIDO='S' WHERE CD_VENDA=123"))
        conn.execute(text("DELETE FROM ENTREGAS WHERE CD_VENDA=123"))
    assert classify_cancellation(repo.fetch_sales_statuses_by_id((123,))) == CancellationDecision.CANCEL
    assert repo.fetch_recent_changes_by_id(0)[-1].action == "DELETE"
