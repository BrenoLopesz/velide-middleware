import pytest
import sqlite3
from datetime import time, date
from sqlalchemy import create_engine, text

from connectors.farmax.farmax_repository import FarmaxRepository
from models.farmax_models import FarmaxDeliveryman

# SQLite doesn't support datetime.time objects natively.
# We register an adapter to convert them to ISO format strings (HH:MM:SS) automatically.
sqlite3.register_adapter(time, lambda t: t.isoformat())


@pytest.fixture
def db_engine():
    engine = create_engine("sqlite:///:memory:")
    with engine.connect() as conn:
        conn.execute(text("PRAGMA foreign_keys=ON"))
    return engine


@pytest.fixture
def repository(db_engine):
    return FarmaxRepository(db_engine)


@pytest.fixture
def setup_database(db_engine):
    with db_engine.begin() as conn:
        # Tables
        conn.execute(
            text(
                "CREATE TABLE ENTREGAS "
                "(CD_VENDA REAL PRIMARY KEY, CD_ENTREGADOR REAL, CD_CLIENTE REAL, " \
                "NOME TEXT, BAIRRO TEXT, DATA TEXT, HORA_SAIDA TEXT, " \
                "HORA_CHEGADA TEXT, STATUS TEXT)"
            )
        )
        conn.execute(
            text(
                "CREATE TABLE VENDAS "
                "('RDB$DB_KEY' INTEGER PRIMARY KEY AUTOINCREMENT, CD_VENDA REAL, " \
                "CD_PRODUTO REAL, DESCRICAO TEXT, HORA TEXT, TEMPENDERECO TEXT, " \
                "TEMPREFERENCIA TEXT, STATUS TEXT, CONCLUIDO TEXT, HORAFINAL TEXT)"
            )
        )
        conn.execute(
            text(
                "CREATE TABLE CLIENTES "
                "(CD_CLIENTE REAL PRIMARY KEY, NOME TEXT, FONE TEXT)"
            )
        )
        conn.execute(
            text(
                "CREATE TABLE VENDEDORES "
                "(CD_VENDEDOR REAL PRIMARY KEY, NOME TEXT, TIPO_FUNCIONARIO TEXT)"
            )
        )

        # Data Injection (Sanitized from your CSVs)
        # Note: Dates are ISO format for SQLite compatibility
        conn.execute(
            text(
                "INSERT INTO VENDAS "
                "(CD_VENDA, CD_PRODUTO, DESCRICAO, HORA, " \
                "TEMPENDERECO, TEMPREFERENCIA, STATUS) " \
                "VALUES "
                "(562083.0, 114830.0, 'ORLISTATE', " \
                "'08:26:07.000', 'RUA BIOLOGOS', 'QDR 70', 'V')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO VENDAS "
                "(CD_VENDA, CD_PRODUTO, DESCRICAO, HORA, TEMPENDERECO, " \
                "TEMPREFERENCIA, STATUS) " \
                "VALUES "
                "(562083.0, 119137.0, 'VAGISIL', '08:26:07.000', " \
                "'RUA BIOLOGOS', 'QDR 70', 'V')"
            )
        )

        # Insert Delivery (Date converted to YYYY-MM-DD)
        conn.execute(
            text(
                "INSERT INTO ENTREGAS "
                "(CD_VENDA, CD_CLIENTE, CD_ENTREGADOR, NOME, " \
                "BAIRRO, STATUS, HORA_SAIDA, DATA) " \
                "VALUES "
                "(562083.0, 1014113.0, 750.0, 'FABI', " \
                "'TAQUARA', 'S', NULL, '2024-03-14')"
            )
        )

        conn.execute(
            text(
                "INSERT INTO CLIENTES (CD_CLIENTE, NOME, FONE) " \
                "VALUES (1014113.0, 'FABI', '996458722')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO VENDEDORES (CD_VENDEDOR, NOME, TIPO_FUNCIONARIO) " \
                "VALUES (870.0, 'RODRIGO NUNES', 'E'), (444.0, 'MARCIO DA SILVA', 'E')"
            )
        )


# ==========================================
# 4. TESTS
# ==========================================


def test_fetch_deliveries_deduplication(repository, setup_database):
    # Should only return 1 delivery despite 2 rows in VENDAS
    results = repository.fetch_deliveries_by_id((562083.0,))
    assert len(results) == 1

    delivery = results[0]
    assert delivery.sale_id == 562083.0
    assert delivery.address == "RUA BIOLOGOS"
    assert delivery.customer_contact == "996458722"
    assert delivery.delivery_date == date(2024, 3, 14)


def test_fetch_deliveries_empty_list(repository, setup_database):
    assert repository.fetch_deliveries_by_id([]) == []


def test_lifecycle_update_status(repository, setup_database, db_engine):
    # 1. Fetch & Verify Initial
    delivery = repository.fetch_deliveries_by_id((562083.0,))[0]

    # 2. Update to "In Route"
    man = FarmaxDeliveryman(cd_vendedor="870", nome="RODRIGO")
    repository.update_delivery_as_in_route(delivery.sale_id, float(man.id), time(10, 30))

    # Verify DB
    with db_engine.connect() as conn:
        row = conn.execute(
            text("SELECT STATUS, CD_ENTREGADOR FROM ENTREGAS WHERE CD_VENDA = 562083.0")
        ).fetchone()
        assert row.STATUS == "R"
        assert row.CD_ENTREGADOR == 870.0

    # 3. Update to "Done"
    repository.update_delivery_as_done(delivery.sale_id, time(11, 0))

    # Verify DB
    with db_engine.connect() as conn:
        row = conn.execute(
            text("SELECT STATUS FROM ENTREGAS WHERE CD_VENDA = 562083.0")
        ).fetchone()
        assert row.STATUS == "V"


def test_fetch_deliverymen(repository, setup_database):
    men = repository.fetch_deliverymen()
    assert len(men) == 2
    assert "RODRIGO NUNES" in [m.name for m in men]
