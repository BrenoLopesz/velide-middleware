"""Read-only Farmax boundary.

The retained native trace does not establish a cashier-independent writer.
Matching delivery fields is an observation, never a claim of cashier settlement.
"""
import logging
import math
from datetime import date, datetime, time
from typing import List, Tuple

import pytz
from pydantic import ValidationError
from sqlalchemy import text

from connectors.farmax.identity import normalize_farmax_id
from connectors.farmax.errors import FarmaxEvidenceError, FarmaxWaitingError, FarmaxMappingMissing
from models.farmax_models import DeliveryLog, FarmaxDelivery, FarmaxDeliveryman, FarmaxSale


def _record(row):
    return {key.lower(): value for key, value in row._mapping.items()}


def _day(value):
    if isinstance(value, datetime):
        return value.date()
    return value if isinstance(value, date) else date.fromisoformat(value)


def _clock(value):
    return value if isinstance(value, time) else time.fromisoformat(value)


class FarmaxRepository:
    LOG_TABLE_NAME = "DELIVERYLOG"
    DELIVERY_FIELDS = "CD_VENDA,CD_FILIAL,CD_CLIENTE,NOME,BAIRRO,DATA,STATUS,CD_ENTREGADOR,HORA_SAIDA,HORA_CHEGADA"
    SALE_FIELDS = "CD_VENDA,CD_FILIAL,CD_CLIENTE,ID_PRODUTO,ORDEM,CD_PRODUTO,DATA_CAIXA,HORA,TEMPENDERECO,TEMPREFERENCIA,STATUS,CONCLUIDO,ENTREGA,TIPO_VENDA,TROCA,PRECO_TOTAL"

    def __init__(self, engine, journal=None, log_available=True):
        self.logger = logging.getLogger(__name__)
        self._engine, self._journal = engine, journal
        self.log_available = log_available
        self._expected_branch = None

    def _build_in_clause_params(self, values):
        return ", ".join(":v{}".format(i) for i in range(len(values))), {"v{}".format(i): value for i, value in enumerate(values)}

    def _branch(self, conn):
        branches = conn.execute(text("SELECT CD_FILIAL FROM PARAMETROS")).fetchall()
        if len(branches) != 1:
            raise FarmaxEvidenceError("Local branch is not a singleton")
        branch = normalize_farmax_id(branches[0][0])
        if self._expected_branch is not None and branch != self._expected_branch:
            raise FarmaxEvidenceError("Configured Farmax branch changed")
        if self._journal:
            self._journal.bind_farmax_branch(branch)
        self._expected_branch = branch
        return branch

    def _snapshot(self, conn, sale_id):
        branch = self._branch(conn)
        deliveries = conn.execute(text("SELECT " + self.DELIVERY_FIELDS + " FROM ENTREGAS WHERE CD_VENDA=:id"), {"id": sale_id}).fetchall()
        if len(deliveries) != 1:
            raise FarmaxEvidenceError("Expected exactly one delivery across all branches")
        delivery = _record(deliveries[0])
        if normalize_farmax_id(delivery["cd_filial"]) != branch:
            raise FarmaxEvidenceError("Delivery is not in the configured branch")
        sales = [_record(row) for row in conn.execute(text("SELECT " + self.SALE_FIELDS + " FROM VENDAS WHERE CD_VENDA=:id"), {"id": sale_id})]
        if not sales or any(normalize_farmax_id(row["cd_filial"]) != branch for row in sales):
            raise FarmaxEvidenceError("Sale is missing or ambiguous across branches")
        keys = [(normalize_farmax_id(row["id_produto"]), normalize_farmax_id(row["ordem"])) for row in sales]
        if len(set(keys)) != len(keys):
            raise FarmaxEvidenceError("Duplicate sale provider keys require review")
        customer = normalize_farmax_id(delivery["cd_cliente"])
        if any(normalize_farmax_id(row["cd_cliente"]) != customer for row in sales):
            raise FarmaxEvidenceError("Sale and delivery customer identities disagree")
        dates = {_day(row["data_caixa"]) for row in sales}
        if len(dates) != 1:
            raise FarmaxEvidenceError("Conflicting sale dates")
        details = {(row["tempendereco"], row["tempreferencia"], _clock(row["hora"]).isoformat()) for row in sales}
        if len(details) != 1:
            raise FarmaxEvidenceError("Conflicting item address/reference/time values")
        address, reference, hour = next(iter(details))
        context = {"branch_id": branch, "customer_id": customer,
                   "delivery_date": _day(delivery["data"]).isoformat(),
                   "sale_date": next(iter(dates)).isoformat(), "item_keys": sorted([list(key) for key in keys]),
                   "address": address, "reference": reference, "sale_time": hour}
        return delivery, sales, context

    @staticmethod
    def _ordinary_sale(sales):
        # A deliberately narrow observed subset, not a complete Farmax status dictionary.
        if any(row["concluido"] != "S" or row["entrega"] != "S" or row["tipo_venda"] != "V"
               or row["status"] not in ("S", "V") or row["troca"] not in (None, "", "N", "F") for row in sales):
            raise FarmaxEvidenceError("Unfinished, returned, canceled, or unsupported sale items require review")
        totals = [float(row["preco_total"]) for row in sales]
        if not all(math.isfinite(value) for value in totals) or sum(totals) <= 0:
            raise FarmaxEvidenceError("A positive, complete ordinary sale total is required")

    def update_delivery_as_in_route(self, *args, **kwargs):
        raise FarmaxEvidenceError("Automatic Farmax departure is disabled: cashier/session and reporting semantics are not qualified")

    def update_delivery_as_done(self, *args, **kwargs):
        raise FarmaxEvidenceError("Automatic Farmax completion is disabled: native return/cashier handoff is not qualified")

    def observe_delivery_effect(self, sale_id, payload, source_timezone):
        try:
            return self._observe_delivery_effect(sale_id, payload, source_timezone)
        except FarmaxEvidenceError:
            raise
        except (ValueError, TypeError, KeyError) as error:
            raise FarmaxEvidenceError("Invalid source provenance: " + str(error)) from error

    def _observe_delivery_effect(self, sale_id, payload, source_timezone):
        """Compare a complete execution with existing source fields; execute no DML."""
        expected = payload.get("source_context")
        if not expected:
            raise FarmaxEvidenceError("Original source provenance is missing")
        zone = pytz.timezone(source_timezone)
        def instant(value):
            if not value:
                raise FarmaxEvidenceError("Original occurrence time is missing")
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if result.tzinfo is None:
                raise FarmaxEvidenceError("Occurrence time must be timezone-aware")
            return result.astimezone(zone)
        started = instant(payload.get("started_at"))
        completed = instant(payload["completed_at"]) if payload.get("completed_at") else None
        with self._engine.connect() as conn:
            delivery, sales, context = self._snapshot(conn, sale_id)
            self._ordinary_sale(sales)
            if context != expected:
                raise FarmaxEvidenceError("Source identity/date/item set changed since intake")
            if context["sale_date"] != context["delivery_date"] or started.date().isoformat() != context["delivery_date"] or (completed and completed.date() != started.date()):
                raise FarmaxEvidenceError("Cross-day or scheduled-delivery correlation requires review")
            if delivery["status"] == "S":
                raise FarmaxWaitingError("Awaiting Farmax-side departure; automatic dispatch is not qualified")
            if not payload.get("courier_local_id"):
                raise FarmaxMappingMissing("Verified historical courier mapping is missing")
            if normalize_farmax_id(delivery["cd_entregador"]) != normalize_farmax_id(payload["courier_local_id"]):
                raise FarmaxEvidenceError("Existing source courier conflicts with the retained execution mapping")
            if delivery["hora_saida"] is None or _clock(delivery["hora_saida"]) != started.time():
                raise FarmaxEvidenceError("Existing departure time does not match; no automatic repair")
            if delivery["status"] == "V" and completed:
                if delivery["hora_chegada"] is None or _clock(delivery["hora_chegada"]) != completed.time():
                    raise FarmaxEvidenceError("Existing arrival time does not match; no automatic repair")
                return "COMPLETED_FIELDS_MATCH"
            if delivery["status"] == "R" and delivery["hora_chegada"] is None:
                if payload["operation"] == "IN_ROUTE":
                    return "DEPARTURE_FIELDS_MATCH"
                raise FarmaxWaitingError("Awaiting Farmax-side return; integration does not finalize it")
            raise FarmaxEvidenceError("Source lifecycle conflicts with the observed remote execution")

    def fetch_sales_statuses_by_id(self, cd_vendas: Tuple[float, ...]) -> List[FarmaxSale]:
        if not cd_vendas:
            return []
        placeholders, params = self._build_in_clause_params(cd_vendas)
        with self._engine.connect() as conn:
            branch = self._branch(conn)
            rows = [_record(row) for row in conn.execute(text("SELECT CD_VENDA,CD_FILIAL,STATUS,CONCLUIDO,TIPO_VENDA FROM VENDAS WHERE CD_VENDA IN (" + placeholders + ")"), params)]
            result = []
            for sale_id in cd_vendas:
                items = [row for row in rows if row["cd_venda"] == sale_id]
                delivery_rows = conn.execute(text("SELECT CD_FILIAL,DATA FROM ENTREGAS WHERE CD_VENDA=:id"), {"id": sale_id}).fetchall()
                logs = []
                if self.log_available:
                    query = self._bounded_log_query("CD_VENDA=:id", descending=True)
                    logs = [_record(row) for row in conn.execute(query, {"id": sale_id})]
                complete = bool(items) and len(delivery_rows) <= 1
                try:
                    complete = complete and all(normalize_farmax_id(row["cd_filial"]) == branch for row in items)
                    complete = complete and all(normalize_farmax_id(row[0]) == branch for row in delivery_rows)
                except ValueError:
                    complete = False
                deleted = bool(logs and logs[0].get("action") == "DELETE")
                for row in items or [{"cd_venda": sale_id, "status": None}]:
                    try:
                        result.append(FarmaxSale.model_validate(dict(row, delivery_exists=bool(delivery_rows),
                            delivery_deleted=deleted, evidence_complete=complete, removal_evidence=logs)))
                    except ValidationError:
                        # Keep an invalid sentinel in this sale's complete group;
                        # never classify only the valid remainder as a whole sale.
                        result.append(FarmaxSale(cd_venda=sale_id, status=None, evidence_complete=False))
            return result

    def fetch_deliveries_by_id(self, cd_vendas, pending_only=True):
        valid = []
        with self._engine.connect() as conn:
            for sale_id in cd_vendas:
                try:
                    delivery, sales, context = self._snapshot(conn, sale_id)
                    if pending_only:
                        if delivery["status"] != "S":
                            raise FarmaxEvidenceError("Source delivery is no longer pending")
                        if any(delivery[field] is not None for field in ("hora_saida", "hora_chegada", "cd_entregador")):
                            raise FarmaxEvidenceError("Pending source retains transition/assignment fields; review its episode")
                        self._ordinary_sale(sales)
                    if context["delivery_date"] != context["sale_date"]:
                        raise FarmaxEvidenceError("Delivery date and sale date differ; timestamp policy requires review")
                    details = {(row["tempendereco"], row["tempreferencia"], str(row["hora"])) for row in sales}
                    if len(details) != 1:
                        raise FarmaxEvidenceError("Conflicting item address/reference/time values")
                    contacts = conn.execute(text("SELECT FONE FROM CLIENTES WHERE CD_CLIENTE=:customer AND CD_FILIAL=:branch"),
                                            {"customer": delivery["cd_cliente"], "branch": delivery["cd_filial"]}).fetchall()
                    if len(contacts) > 1:
                        raise FarmaxEvidenceError("Ambiguous composite customer identity")
                    address, reference, hour = next(iter(details))
                    data = dict(delivery, tempendereco=address, tempreferencia=reference, hora=hour,
                                fone=contacts[0][0] if contacts else None, source_context=context)
                    valid.append(FarmaxDelivery.model_validate(data))
                except (ValueError, TypeError) as error:
                    if not self._journal:
                        raise
                    self._journal.resolve_source("farmax:" + normalize_farmax_id(sale_id), str(error))
                    self.logger.error("Farmax source %s requires review: %s", sale_id, error)
        return valid

    def _read_logs(self, predicate, params):
        if not self.log_available:
            return []
        with self._engine.connect() as conn:
            rows = [_record(row) for row in conn.execute(self._bounded_log_query(predicate), params)]
        valid = []
        ids = [row.get("id") for row in rows]
        for row in rows:
            try:
                if ids.count(row.get("id")) != 1:
                    raise ValueError("Duplicate log IDs: ordering is unqualified")
                valid.append(DeliveryLog.model_validate(row))
            except (ValueError, TypeError) as error:
                if not self._journal:
                    raise
                key = "invalid-log:" + str(row.get("id"))
                self._journal.observe_source([(key, row)])
                self._journal.resolve_source(key, str(error))
        return valid

    def fetch_recent_changes(self, last_check_time):
        return self._read_logs("LOGDATE >= :since", {"since": last_check_time})

    def fetch_recent_changes_by_id(self, last_id):
        return self._read_logs("ID > :id", {"id": max(0, last_id - 100)})

    def _bounded_log_query(self, predicate, descending=False):
        prefix = "FIRST 500 " if self._engine.dialect.name.startswith("firebird") else ""
        suffix = "" if prefix else " LIMIT 500"
        return text("SELECT " + prefix + "ID,CD_VENDA,ACTION,LOGDATE FROM DELIVERYLOG WHERE " + predicate
                    + " ORDER BY ID" + (" DESC" if descending else "") + suffix)

    def fetch_pending_ids(self, after_id=0):
        prefix = "FIRST 100 " if self._engine.dialect.name.startswith("firebird") else ""
        suffix = "" if prefix else " LIMIT 100"
        with self._engine.connect() as conn:
            self._branch(conn)
            return [row[0] for row in conn.execute(text("SELECT " + prefix + "DISTINCT CD_VENDA FROM ENTREGAS WHERE STATUS='S' AND CD_VENDA>:after_id ORDER BY CD_VENDA" + suffix), {"after_id": after_id})]

    def fetch_deliverymen(self):
        with self._engine.connect() as conn:
            branch = self._branch(conn)
            return [FarmaxDeliveryman(cd_vendedor=str(row[0]), nome=row[1]) for row in conn.execute(text(
                "SELECT CD_VENDEDOR,NOME FROM VENDEDORES WHERE TIPO_FUNCIONARIO='E' AND STATUS='A' AND CD_FILIAL=:branch ORDER BY NOME"), {"branch": int(branch)})]
