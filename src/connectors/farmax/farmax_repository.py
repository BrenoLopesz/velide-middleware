from datetime import datetime, time
import logging
from textwrap import dedent
from typing import List, Tuple
from pydantic import ValidationError

from sqlalchemy import text, Engine
from sqlalchemy.exc import SQLAlchemyError
from models.farmax_models import (
    DeliveryLog,
    FarmaxDelivery,
    FarmaxDeliveryman,
    FarmaxSale,
)


class FarmaxRepository:
    LOG_TABLE_NAME = "DELIVERYLOG"

    def __init__(self, engine: Engine, journal=None):
        self.logger = logging.getLogger(__name__)
        self._engine = engine
        self._journal = journal

    def _build_in_clause_params(self, values: Tuple) -> Tuple[str, dict]:
        """Builds placeholders and params for an IN clause."""
        if not values:
            # Return a clause that will result in an empty query
            # Using "1=0" is a common trick, but might not be
            # necessary if you already check for empty.
            # Here, we'll just return for the existing check.
            return "", {}

        placeholders = ", ".join([f":v{i}" for i in range(len(values))])
        params = {f"v{i}": v for i, v in enumerate(values)}
        return placeholders, params

    def update_delivery_as_in_route(
        self, sale_id: float, driver_id: float, left_at: time, historical: bool = False
    ):
        """
        Updates the delivery status to 'R' (Route) and assigns the driver.
        """
        query = text(
            "UPDATE ENTREGAS SET CD_ENTREGADOR = :man_id, HORA_SAIDA = :time, "
            "STATUS = 'R' WHERE CD_VENDA = :sale_id AND STATUS IN ('S','R')"
        )
        params = {"man_id": driver_id, "time": left_at, "sale_id": sale_id}

        try:
            with self._engine.begin() as conn:
                state = self._read_write_state(conn, sale_id)
                if state["status"] not in ("S", "R"):
                    raise ValueError("Departure conflicts with current Farmax delivery state")
                if (state["status"] == "R" and state["cd_entregador"] == driver_id
                        and str(state["hora_saida"]) == str(left_at)):
                    return
                eligible = conn.execute(text("SELECT CD_VENDEDOR FROM VENDEDORES WHERE CD_VENDEDOR=:driver_id "
                    "AND CD_FILIAL=(SELECT CD_FILIAL FROM PARAMETROS) AND TIPO_FUNCIONARIO='E' AND (STATUS='A' OR :historical=1)"),
                    {"driver_id": driver_id, "historical": int(historical)}).fetchall()
                if len(eligible) != 1:
                    raise ValueError("Courier is not active in the configured Farmax branch")
                if conn.execute(query, params).rowcount != 1:
                    raise ValueError("Departure did not update exactly one delivery")
        except SQLAlchemyError:
            self.logger.exception(
                f"Falha ao atualizar entrega {sale_id} para 'Em Rota'."
            )
            raise
        except Exception:
            self.logger.exception(
                f"Erro inesperado ao atualizar entrega {sale_id} para Em Rota."
            )
            raise

    def update_delivery_as_done(self, sale_id: float, ended_at: time):
        """
        Atomically updates a delivery to 'Done' status in both ENTREGAS and VENDAS.
        """
        update_entregas_query = text(
            "UPDATE ENTREGAS SET HORA_CHEGADA = :time, STATUS = 'V' "
            "WHERE CD_VENDA = :sale_id AND STATUS IN ('S','R','V')"
        )
        update_vendas_query = text(
            "UPDATE VENDAS SET CONCLUIDO = 'S', STATUS = 'V', HORAFINAL = :time "
            "WHERE CD_VENDA = :sale_id AND STATUS IN ('S','V')"
        )
        params = {"time": ended_at, "sale_id": sale_id}

        try:
            with self._engine.begin() as conn:
                state = self._read_write_state(conn, sale_id)
                if state["status"] not in ("R", "V"):
                    raise ValueError("Completion conflicts with current Farmax delivery state")
                if state["status"] == "V":
                    if str(state["hora_chegada"]) != str(ended_at):
                        raise ValueError("Completion timestamp conflicts with an existing completion")
                    return  # Firebird committed this effect before its local acknowledgement.
                if conn.execute(update_entregas_query, params).rowcount != 1:
                    raise ValueError("Completion did not update exactly one delivery")
                if conn.execute(update_vendas_query, params).rowcount < 1:
                    raise ValueError("Completion did not update any eligible sale items")
        except SQLAlchemyError:
            self.logger.exception(f"Falha ao finalizar entrega {sale_id}.")
            raise
        except Exception:
            self.logger.exception(f"Erro inesperado ao finalizar entrega {sale_id}.")
            raise

    def _read_write_state(self, conn, sale_id):
        suffix = " WITH LOCK" if self._engine.dialect.name.startswith("firebird") else ""
        rows = conn.execute(text("SELECT STATUS, CD_ENTREGADOR, HORA_SAIDA, HORA_CHEGADA "
                                 "FROM ENTREGAS WHERE CD_VENDA=:sale_id" + suffix),
                            {"sale_id": sale_id}).fetchall()
        if len(rows) != 1:
            raise ValueError("Expected exactly one Farmax delivery for this sale")
        statuses = conn.execute(text("SELECT STATUS FROM VENDAS WHERE CD_VENDA=:sale_id"),
                                {"sale_id": sale_id}).fetchall()
        if not statuses or not any(row[0] in ("S", "V") for row in statuses):
            raise ValueError("No active sale items remain for this delivery")
        return {key.lower(): value for key, value in rows[0]._mapping.items()}

    def fetch_sales_statuses_by_id(
        self, cd_vendas: Tuple[float, ...]
    ) -> List[FarmaxSale]:
        """
        Fetches the status for a given list of sale IDs.
        Returns an empty list if no IDs are provided.
        """
        # Handle empty IN-clause
        if not cd_vendas:
            return []

        placeholders, params = self._build_in_clause_params(cd_vendas)

        query = text(
            f"SELECT V.CD_VENDA, V.STATUS, V.CONCLUIDO, "
            f"CASE WHEN EXISTS (SELECT 1 FROM ENTREGAS E "
            f"WHERE E.CD_VENDA = V.CD_VENDA) THEN 1 ELSE 0 END AS DELIVERY_EXISTS, "
            f"CASE WHEN EXISTS (SELECT 1 FROM {self.LOG_TABLE_NAME} L "
            f"WHERE L.CD_VENDA = V.CD_VENDA AND L.ACTION = 'DELETE' "
            f"AND L.ID = (SELECT MAX(L2.ID) FROM {self.LOG_TABLE_NAME} L2 "
            f"WHERE L2.CD_VENDA = V.CD_VENDA)) THEN 1 ELSE 0 END AS DELIVERY_DELETED "
            f"FROM VENDAS V WHERE V.CD_VENDA IN ({placeholders}) "
            f"ORDER BY V.CD_VENDA DESC"
        )

        with self._engine.connect() as conn:
            result = conn.execute(query, params)
            rows = result.fetchall()
            row_dicts = [
                {k.lower(): v for k, v in row._mapping.items()} for row in rows
            ]
            return [FarmaxSale.model_validate(row_dict) for row_dict in row_dicts]

    def fetch_deliveries_by_id(
        self, cd_vendas: Tuple[float, ...], pending_only: bool = True
    ) -> List[FarmaxDelivery]:
        """
        Fetches detailed delivery info from both ENTREGAS and VENDAS
        in a single query.
        """
        if not cd_vendas:
            return []

        placeholders, params = self._build_in_clause_params(cd_vendas)

        # This query merges the logic from both functions
        query_str = dedent(f"""
            SELECT
                E.CD_VENDA,
                E.NOME,
                E.HORA_SAIDA,
                E.BAIRRO,
                E.DATA,
                V_Details.HORA,
                V_Details.TEMPENDERECO,
                V_Details.TEMPREFERENCIA,
                C.FONE
            FROM
                ENTREGAS E
            LEFT JOIN (
                -- This is the entire 'enhance' subquery to find the primary VENDAS row
                SELECT
                    V1.CD_VENDA,
                    V1.HORA,
                    V1.TEMPENDERECO,
                    V1.TEMPREFERENCIA
                    -- Note: If CD_CLIENTE is on VENDAS instead of ENTREGAS,
                    -- you would add V1.CD_CLIENTE here and join C to V_Details later.
                FROM
                    VENDAS V1
                JOIN (
                    SELECT
                        V_inner.CD_VENDA,
                        MIN(V_inner.RDB$DB_KEY) AS unique_row_key
                    FROM
                        VENDAS V_inner
                    JOIN (
                        SELECT
                            CD_VENDA,
                            MIN(CD_PRODUTO) AS first_sale
                        FROM
                            VENDAS
                        WHERE
                            CD_VENDA IN ({placeholders}) -- Filter subquery
                        GROUP BY
                            CD_VENDA
                    ) V2
                        ON V_inner.CD_VENDA = V2.CD_VENDA
                    AND V_inner.CD_PRODUTO = V2.first_sale
                    GROUP BY
                        V_inner.CD_VENDA
                ) V3
                    ON V1.RDB$DB_KEY = V3.unique_row_key
            ) AS V_Details ON E.CD_VENDA = V_Details.CD_VENDA
            
            -- vvv 2. ADDED THIS JOIN vvv
            LEFT JOIN CLIENTES C ON E.CD_CLIENTE = C.CD_CLIENTE
            
            WHERE
                {"E.STATUS = 'S' AND" if pending_only else ""} E.CD_VENDA IN ({placeholders})
            ORDER BY
                E.CD_VENDA DESC
        """)

        with self._engine.connect() as conn:
            result = conn.execute(text(query_str), params)
            rows = result.fetchall()
            row_dicts = [
                {k.lower(): v for k, v in row._mapping.items()} for row in rows
            ]

            valid = []
            for row_dict in row_dicts:
                try:
                    valid.append(FarmaxDelivery.model_validate(row_dict))
                except ValidationError as error:
                    if not self._journal:
                        raise
                    from connectors.farmax.identity import normalize_farmax_id
                    key = "farmax:" + normalize_farmax_id(row_dict["cd_venda"])
                    self._journal.resolve_source(key, str(error))
                    self.logger.error("Invalid Farmax source record %s: %s", key, error)
            return valid

    def fetch_recent_changes(self, last_check_time: datetime) -> List[DeliveryLog]:
        """
        Fetches all delivery log changes since the last check time.
        """
        query = self._bounded_log_query("LOGDATE >= :last_check")

        with self._engine.connect() as conn:
            result = conn.execute(query, {"last_check": last_check_time})
            rows = result.fetchall()
            row_dicts = [
                {k.lower(): v for k, v in row._mapping.items()} for row in rows
            ]
           
            valid_logs = []
            for row_dict in row_dicts:
                try:
                    # Try to validate the row
                    log = DeliveryLog.model_validate(row_dict)
                    valid_logs.append(log)
                except ValidationError as e:
                    self._quarantine_log(row_dict, e)
                    # If validation fails, skip this row and continue.
                    # It is highly recommended to log this failure so you know data is missing.
                    self.logger.exception(
                        "Entrega possui formatação inválida! Prosseguindo..."
                    )
                    continue
                    
            return valid_logs

    def fetch_recent_changes_by_id(self, last_id: int) -> List[DeliveryLog]:
        """
        Fetches all delivery log changes since the last check time.
        """
        query = self._bounded_log_query("ID > :last_id")

        with self._engine.connect() as conn:
            result = conn.execute(query, {"last_id": max(0, last_id - 100)})
            rows = result.fetchall()
            row_dicts = [
                {k.lower(): v for k, v in row._mapping.items()} for row in rows
            ]

            valid_logs = []
            for row_dict in row_dicts:
                try:
                    # Try to validate the row
                    log = DeliveryLog.model_validate(row_dict)
                    valid_logs.append(log)
                except ValidationError as e:
                    self._quarantine_log(row_dict, e)
                    # If validation fails, skip this row and continue.
                    # It is highly recommended to log this failure so you know data is missing.
                    self.logger.exception(
                        "Entrega possui formatação inválida! Prosseguindo..."
                    )
                    continue
                    
            return valid_logs

    def _bounded_log_query(self, predicate):
        if self._engine.dialect.name.startswith("firebird"):
            return text(f"SELECT FIRST 500 * FROM {self.LOG_TABLE_NAME} WHERE {predicate} ORDER BY ID")
        return text(f"SELECT * FROM {self.LOG_TABLE_NAME} WHERE {predicate} ORDER BY ID LIMIT 500")

    def _quarantine_log(self, row, error):
        if not self._journal:
            raise error
        key = "invalid-log:" + str(row.get("id"))
        self._journal.observe_source([(key, row)])
        self._journal.resolve_source(key, str(error))

    def fetch_pending_ids(self, after_id=0):
        prefix = "FIRST 100 " if self._engine.dialect.name.startswith("firebird") else ""
        suffix = "" if prefix else " LIMIT 100"
        query = text(f"SELECT {prefix}DISTINCT CD_VENDA FROM ENTREGAS "
                     "WHERE STATUS='S' AND CD_VENDA>:after_id ORDER BY CD_VENDA" + suffix)
        with self._engine.connect() as conn:
            return [row[0] for row in conn.execute(query, {"after_id": after_id})]

    def fetch_deliverymen(self) -> List[FarmaxDeliveryman]:
        """
        Fetches all active deliverymen.
        """
        query = text(
            "SELECT CD_VENDEDOR, NOME FROM VENDEDORES "
            "WHERE TIPO_FUNCIONARIO = 'E' "
            "AND STATUS='A' AND CD_FILIAL=(SELECT CD_FILIAL FROM PARAMETROS) "
            "ORDER BY NOME"
        )

        with self._engine.connect() as conn:
            result = conn.execute(query)
            rows = result.fetchall()

            deliverymen = []
            for row in rows:
                # Manually create a dictionary.
                data_dict = {"cd_vendedor": row[0], "nome": row[1]}
                deliverymen.append(FarmaxDeliveryman.model_validate(data_dict))
            return deliverymen
