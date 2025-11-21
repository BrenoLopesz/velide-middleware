from datetime import datetime, time
import logging
from textwrap import dedent
from typing import List, Tuple

from sqlalchemy import text, Engine
from sqlalchemy.exc import SQLAlchemyError
from models.farmax_models import DeliveryLog, FarmaxDelivery, FarmaxDeliveryman, FarmaxSale

class FarmaxRepository:
    LOG_TABLE_NAME = "DELIVERYLOG"

    def __init__(self, engine: Engine):
        self.logger = logging.getLogger(__name__)
        self._engine = engine

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

    def update_delivery_as_in_route(self, delivery: FarmaxDelivery, deliveryman: FarmaxDeliveryman, left_at: time):
        query = text(
            "UPDATE ENTREGAS SET CD_ENTREGADOR = :man_id, HORA_SAIDA = :time, "
            "STATUS = 'R' WHERE CD_VENDA = :sale_id"
        )
        params = {
            "man_id": deliveryman.id,
            "time": left_at,
            "sale_id": delivery.sale_id
        }

        try: 
            with self._engine.begin() as conn:
                conn.execute(query, params)
        except SQLAlchemyError:
            self.logger.exception(f"Falha ao atualizar entrega após rota iniciar.")
            raise
        except Exception:
            self.logger.exception(f"Um erro inesperado ocorreu ao atualizar entrega após rota iniciar.")
            raise
    
    def update_delivery_as_done(self, delivery: FarmaxDelivery, ended_at: time):
        """Atomically updates a delivery to 'Done' status in both ENTREGAS and VENDAS."""
    
        # Both queries will pick the parameters they need from the 'params' dict.
        update_entregas_query = text(
            "UPDATE ENTREGAS SET HORA_CHEGADA = :time, STATUS = 'V' "
            "WHERE CD_VENDA = :sale_id"
        )
        update_vendas_query = text(
            "UPDATE VENDAS SET CONCLUIDO = 'S', STATUS = 'V', HORAFINAL = :time "
            "WHERE CD_VENDA = :sale_id"
        )
        params = {
            "time": ended_at,
            "sale_id": delivery.sale_id
        }

        try:
            with self._engine.begin() as conn:
                conn.execute(update_entregas_query, params)
                conn.execute(update_vendas_query, params)
        except SQLAlchemyError:
            self.logger.exception(f"Falha ao atualizar entrega após rota finalizar.")
            raise
        except Exception:
            self.logger.exception(f"Um erro inesperado ocorreu ao atualizar entrega após rota finalizar.")
            raise

    def fetch_sales_statuses_by_id(self, cd_vendas: Tuple[float]) -> List[FarmaxSale]:
        """
        Fetches the status for a given list of sale IDs.
        Returns an empty list if no IDs are provided.
        """
        # Handle empty IN-clause
        if not cd_vendas:
            return []
            
        placeholders, params = self._build_in_clause_params(cd_vendas)

        query = text(
            f"SELECT CD_VENDA, STATUS FROM VENDAS "
            f"WHERE CD_VENDA IN ({placeholders}) "
            f"ORDER BY CD_VENDA DESC"
        )
        
        with self._engine.connect() as conn:
            result = conn.execute(query, params)
            rows = result.fetchall()
            row_dicts = [dict(row._mapping) for row in rows]
            return [FarmaxSale.model_validate(row_dict) for row_dict in row_dicts]

    def fetch_deliveries_by_id(self, cd_vendas: Tuple[float]) -> List[FarmaxDelivery]:
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
                E.STATUS = 'S' AND E.CD_VENDA IN ({placeholders}) -- Filter main query
            ORDER BY
                E.CD_VENDA DESC
        """)

        with self._engine.connect() as conn:
            result = conn.execute(text(query_str), params)
            rows = result.fetchall()
            row_dicts = [dict(row._mapping) for row in rows]
            
            # Directly convert the complete rows to Pydantic models
            return [FarmaxDelivery.model_validate(row_dict) for row_dict in row_dicts]

    def fetch_recent_changes(self, last_check_time: datetime) -> List[DeliveryLog]:
        """
        Fetches all delivery log changes since the last check time.
        """
        query = text(
            f"SELECT * FROM {self.LOG_TABLE_NAME} "
            f"WHERE LOGDATE > :last_check"
        )
        
        with self._engine.connect() as conn:
            result = conn.execute(query, {"last_check": last_check_time})
            rows = result.fetchall()
            row_dicts = [dict(row._mapping) for row in rows]
            return [DeliveryLog.model_validate(row_dict) for row_dict in row_dicts]
        
    def fetch_recent_changes_by_id(self, last_id: int) -> List[DeliveryLog]:
        """
        Fetches all delivery log changes since the last check time.
        """
        query = text(
            f"SELECT * FROM {self.LOG_TABLE_NAME} "
            f"WHERE ID > :last_id"
        )
        
        with self._engine.connect() as conn:
            result = conn.execute(query, {"last_id": last_id})
            rows = result.fetchall()
            row_dicts = [dict(row._mapping) for row in rows]
            return [DeliveryLog.model_validate(row_dict) for row_dict in row_dicts]

    def fetch_deliverymen(self) -> List[FarmaxDeliveryman]:
        """
        Fetches all active deliverymen.
        """
        query = text(
            "SELECT CD_VENDEDOR, NOME FROM VENDEDORES "
            "WHERE TIPO_FUNCIONARIO = 'E' "
            "ORDER BY NOME"
        )
        
        with self._engine.connect() as conn:
            result = conn.execute(query)
            rows = result.fetchall()

            deliverymen = []
            for row in rows:
                # Manually create a dictionary.
                data_dict = {
                    'cd_vendedor': row[0],
                    'nome': row[1]
                }
                deliverymen.append(FarmaxDeliveryman.model_validate(data_dict))
            return deliverymen