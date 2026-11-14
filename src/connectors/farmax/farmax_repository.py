"""Velide-led delivery projection and complete Farmax cancellation snapshots.

Delivery writes are atomic and deliberately exclude cashier transfer/settlement.
Matching fields acknowledges this projection, not the entire native ERP workflow.
"""
import logging
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

    def _snapshot(self, conn, sale_id, lock=False):
        branch = self._branch(conn)
        suffix = " WITH LOCK" if lock and self._engine.dialect.name.startswith("firebird") else ""
        deliveries = conn.execute(text("SELECT " + self.DELIVERY_FIELDS + " FROM ENTREGAS WHERE CD_VENDA=:id" + suffix), {"id": sale_id}).fetchall()
        if len(deliveries) != 1:
            raise FarmaxEvidenceError("Expected exactly one delivery across all branches")
        delivery = _record(deliveries[0])
        if normalize_farmax_id(delivery["cd_filial"]) != branch:
            raise FarmaxEvidenceError("Delivery is not in the configured branch")
        sales = [_record(row) for row in conn.execute(text("SELECT " + self.SALE_FIELDS + " FROM VENDAS WHERE CD_VENDA=:id" + suffix), {"id": sale_id})]
        context = self._sale_context(sales, branch, delivery)
        return delivery, sales, context

    @staticmethod
    def _sale_context(sales, branch, delivery=None):
        if not sales or any(normalize_farmax_id(row["cd_filial"]) != branch for row in sales):
            raise FarmaxEvidenceError("Sale is missing or ambiguous across branches")
        sale_ids = {normalize_farmax_id(row["cd_venda"]) for row in sales}
        if len(sale_ids) != 1 or (delivery and normalize_farmax_id(delivery["cd_venda"]) not in sale_ids):
            raise FarmaxEvidenceError("Conflicting sale identities")
        keys = [(normalize_farmax_id(row["id_produto"]), normalize_farmax_id(row["ordem"])) for row in sales]
        if len(set(keys)) != len(keys):
            raise FarmaxEvidenceError("Duplicate sale provider keys require review")
        customer = normalize_farmax_id((delivery or sales[0])["cd_cliente"])
        if any(normalize_farmax_id(row["cd_cliente"]) != customer for row in sales):
            raise FarmaxEvidenceError("Sale and delivery customer identities disagree")
        dates = {_day(row["data_caixa"]) for row in sales}
        if len(dates) != 1:
            raise FarmaxEvidenceError("Conflicting sale dates")
        details = {(row["tempendereco"], row["tempreferencia"], _clock(row["hora"]).isoformat()) for row in sales}
        if len(details) != 1:
            raise FarmaxEvidenceError("Conflicting item address/reference/time values")
        address, reference, hour = next(iter(details))
        context = {"branch_id": branch, "sale_id": next(iter(sale_ids)), "customer_id": customer,
                   "delivery_date": _day(delivery["data"]).isoformat() if delivery else None,
                   "sale_date": next(iter(dates)).isoformat(), "item_keys": sorted([list(key) for key in keys]),
                   "address": address, "reference": reference, "sale_time": hour}
        return context

    @staticmethod
    def _ordinary_sale(sales):
        # C/D/I rows remain in identity/progress snapshots but are never revived.
        # CONCLUIDO and HORAFINAL are checkout-owned, independent of courier return.
        # SP_RETORNA_ENTREGAS excludes type D; it does not whitelist V/P/R/M.
        # Its aggregate total/checkout filters describe a UI list, not permission
        # to project an existing ENTREGAS row from an authoritative Velide route.
        if any(row["status"] not in ("S", "V", "C", "D", "I") for row in sales):
            raise FarmaxEvidenceError("Unknown sale item status")
        active = [row for row in sales if row["status"] in ("S", "V") and row["tipo_venda"] != "D"]
        if not active:
            raise FarmaxEvidenceError("Sale has no active delivery items; reconcile cancellation")
        return active

    @staticmethod
    def _check_source_context(sale_id, current, expected, legacy_adopted=False):
        """Compare identity, accepting old contexts without the added sale_id.

        Only the journal's proven ORIGINAL-installation adoption can bootstrap
        missing provenance. It does not waive a supplied conflicting identity.
        Item-key changes wait for correlated progress to refresh the payload;
        statuses, sale clocks and ancillary metadata are not stable identity.
        """
        if expected is None or expected == {}:
            if legacy_adopted is True:
                return
            raise FarmaxEvidenceError("Original source provenance is missing")
        if not isinstance(expected, dict):
            raise FarmaxEvidenceError("Invalid original source provenance")
        for field in ("branch_id", "sale_id", "customer_id", "sale_date", "delivery_date", "address"):
            if field == "sale_id" and field not in expected:
                value = sale_id
            elif field not in expected:
                if legacy_adopted is True:
                    continue
                raise FarmaxEvidenceError("Original source identity field is missing: " + field)
            else:
                value = expected[field]
            if field in ("branch_id", "sale_id", "customer_id"):
                value = normalize_farmax_id(value)
            elif field in ("sale_date", "delivery_date"):
                value = _day(value).isoformat()
            if value != current[field]:
                raise FarmaxEvidenceError("Source identity changed since intake: " + field)
        if "item_keys" in expected:
            keys = expected["item_keys"]
            if not isinstance(keys, (list, tuple)) or not keys or any(
                    not isinstance(key, (list, tuple)) or len(key) != 2 for key in keys):
                raise FarmaxEvidenceError("Invalid retained sale item keys")
            normalized = [tuple(normalize_farmax_id(value) for value in key) for key in keys]
            if len(set(normalized)) != len(normalized):
                raise FarmaxEvidenceError("Duplicate retained sale item keys")
            if sorted(normalized) != sorted(tuple(key) for key in current["item_keys"]):
                raise FarmaxWaitingError("Awaiting correlated source item set refresh")

    def update_delivery_as_in_route(self, sale_id, driver_id, left_at, historical=False):
        """Legacy clock-only caller; durable callers should use apply_delivery_effect."""
        sale_id = int(normalize_farmax_id(sale_id))
        with self._engine.begin() as conn:
            snapshot = self._snapshot(conn, sale_id, lock=True)
            return self._project(conn, sale_id, snapshot, "IN_ROUTE", driver_id, _clock(left_at), None, True)

    def update_delivery_as_done(self, sale_id, ended_at):
        sale_id = int(normalize_farmax_id(sale_id))
        with self._engine.begin() as conn:
            snapshot = self._snapshot(conn, sale_id, lock=True)
            delivery = snapshot[0]
            if delivery["hora_saida"] is None:
                raise FarmaxWaitingError("Original departure time is required")
            return self._project(conn, sale_id, snapshot, "DONE", delivery["cd_entregador"],
                                 _clock(delivery["hora_saida"]), _clock(ended_at), True)

    def apply_delivery_effect(self, sale_id, payload, source_timezone, authority_check=None):
        """Apply/reconcile an authoritative Velide execution in one transaction.

        Payload: operation IN_ROUTE/DONE, source_context from intake (or journal
        adoption), courier_local_id, aware started_at and, for DONE, completed_at.
        legacy_adopted=True is reserved for journal-proven original installations
        whose mappings predate source_context; other missing provenance fails.
        TIME columns store the original local clocks even across dates; no ERP
        business date, receipt time, cashier or sale-conclusion value is invented.
        """
        return self._delivery_effect(sale_id, payload, source_timezone, apply=True, authority_check=authority_check)

    def observe_delivery_effect(self, sale_id, payload, source_timezone):
        """Read-only diagnosis using the same projection rules as the writer."""
        return self._delivery_effect(sale_id, payload, source_timezone, apply=False)

    def _delivery_effect(self, sale_id, payload, source_timezone, apply, authority_check=None):
        try:
            return self._execute_delivery_effect(sale_id, payload, source_timezone, apply, authority_check)
        except FarmaxEvidenceError:
            raise
        except (ValueError, TypeError, KeyError) as error:
            raise FarmaxEvidenceError("Invalid source provenance: " + str(error)) from error

    def _execute_delivery_effect(self, sale_id, payload, source_timezone, apply, authority_check=None):
        sale_id = int(normalize_farmax_id(sale_id))
        expected = payload.get("source_context")
        zone = pytz.timezone(source_timezone)
        def instant(value):
            if not value:
                raise FarmaxEvidenceError("Original occurrence time is missing")
            result = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
            if result.tzinfo is None:
                raise FarmaxEvidenceError("Occurrence time must be timezone-aware")
            return result.astimezone(zone)
        started = instant(payload.get("started_at"))
        completed = instant(payload["completed_at"]) if payload.get("completed_at") else None
        operation = payload.get("operation")
        if operation not in ("IN_ROUTE", "DONE") or (operation == "DONE" and completed is None):
            raise FarmaxEvidenceError("A supported operation and its original occurrence time are required")
        if completed and completed < started:
            raise FarmaxEvidenceError("Completion precedes departure")
        with self._engine.begin() as conn:
            snapshot = self._snapshot(conn, sale_id, lock=apply)
            if authority_check is not None and not authority_check():
                raise FarmaxWaitingError("A situação da rota mudou. Aguardando os dados atuais do Velide.")
            context = snapshot[2]
            self._check_source_context(sale_id, context, expected, payload.get("legacy_adopted"))
            result = self._project(conn, sale_id, snapshot, operation, payload.get("courier_local_id"),
                                   started.time(), completed.time() if completed else None, apply)
            if authority_check is not None and not authority_check():
                raise FarmaxWaitingError("A situação da rota mudou. A atualização do Farmax será refeita com os dados atuais.")
            return result

    @staticmethod
    def _stored_clock(value):
        # Firebird TIME has 100-microsecond precision. Truncate deterministically
        # so replay of the original Velide instant matches the persisted value.
        return value.replace(microsecond=(value.microsecond // 100) * 100)

    @staticmethod
    def _field_value(row, field):
        value = row[field]
        return _clock(value) if value is not None and field.startswith("hora_") else value

    def _project(self, conn, sale_id, snapshot, operation, courier, started, completed, apply):
        delivery, sales, context = snapshot
        active = self._ordinary_sale(sales)
        if courier is None or not str(courier).strip():
            raise FarmaxMappingMissing("Verified historical courier mapping is missing")
        courier = int(normalize_farmax_id(courier))
        if courier <= 0:
            raise FarmaxMappingMissing("A positive courier mapping is required")
        started = self._stored_clock(started)
        completed = self._stored_clock(completed) if completed else None
        status = delivery["status"]
        if status not in ("S", "R", "V"):
            raise FarmaxEvidenceError("Unsupported delivery lifecycle state")
        # An ERP return alone is never a synthetic Velide route completion.
        finish = operation == "DONE" or (status == "V" and completed is not None)
        if status == "V" and not finish:
            raise FarmaxWaitingError("Awaiting authoritative Velide completion for an existing Farmax return")
        target = "V" if finish else "R"
        arrival = delivery["hora_chegada"]
        # Velide establishes courier/departure for an open S/R delivery, even
        # when a Farmax operator previously entered different departure fields.
        desired = {"status": target, "cd_entregador": courier, "hora_saida": started}
        if status == "V":
            # A closed native return is historical evidence. Accept Velide DONE
            # without rewriting populated native departure/arrival fields; fill
            # only missing fields and complete any remaining eligible items.
            if delivery["cd_entregador"] not in (None, 0):
                desired["cd_entregador"] = delivery["cd_entregador"]
            if delivery["hora_saida"] is not None:
                desired["hora_saida"] = _clock(delivery["hora_saida"])
        if finish:
            desired["hora_chegada"] = _clock(arrival) if arrival is not None else completed
        changed = {field: value for field, value in desired.items()
                   if self._field_value(delivery, field) != value}
        pending_items = [row for row in active if row["status"] != "V"] if finish else []
        if not apply and (changed or pending_items):
            raise FarmaxWaitingError("Delivery projection is pending Velide-led source writes")
        if apply and changed:
            params = dict(changed, id=sale_id, branch=int(context["branch_id"]), old_status=status)
            result = conn.execute(text("UPDATE ENTREGAS SET " + ", ".join(field.upper() + "=:" + field for field in changed)
                                       + " WHERE CD_VENDA=:id AND CD_FILIAL=:branch AND STATUS=:old_status"), params)
            if result.rowcount != 1:
                raise FarmaxEvidenceError("Delivery changed concurrently or has ambiguous identity")
        if apply and pending_items:
            # Native ordering: delivery first, then reload/apply each sale item.
            current = [_record(row) for row in conn.execute(text("SELECT " + self.SALE_FIELDS + " FROM VENDAS WHERE CD_VENDA=:id"), {"id": sale_id})]
            if self._sale_context(current, context["branch_id"], delivery) != context:
                raise FarmaxEvidenceError("Sale identity changed during delivery update")
            for row in self._ordinary_sale(current):
                if row["status"] == "V":
                    continue
                type_predicate = "TIPO_VENDA IS NULL" if row["tipo_venda"] is None else "TIPO_VENDA=:sale_type"
                result = conn.execute(text("UPDATE VENDAS SET STATUS='V' WHERE CD_VENDA=:id AND CD_FILIAL=:branch "
                                           "AND ID_PRODUTO=:item AND ORDEM=:position AND STATUS=:old_status AND " + type_predicate),
                                      {"id": sale_id, "branch": int(context["branch_id"]), "item": row["id_produto"],
                                       "position": row["ordem"], "old_status": row["status"], "sale_type": row["tipo_venda"]})
                if result.rowcount != 1:
                    raise FarmaxEvidenceError("Sale item changed concurrently or has ambiguous identity")
        if apply and (changed or pending_items):
            # An installed custom trigger can alter the assigned fields. Never
            # acknowledge a successful execute as a matching persisted effect.
            final_delivery, final_sales, final_context = self._snapshot(conn, sale_id)
            if final_context != context or any(self._field_value(final_delivery, field) != value for field, value in desired.items()):
                raise FarmaxEvidenceError("Persisted delivery projection does not match the requested effect")
            if finish and any(row["status"] != "V" for row in self._ordinary_sale(final_sales)):
                raise FarmaxEvidenceError("Persisted sale-item projection does not match completion")
        return "COMPLETED_FIELDS_MATCH" if finish else "DEPARTURE_FIELDS_MATCH"

    def fetch_sales_statuses_by_id(self, cd_vendas: Tuple[float, ...]) -> List[FarmaxSale]:
        if not cd_vendas:
            return []
        placeholders, params = self._build_in_clause_params(cd_vendas)
        with self._engine.connect() as conn:
            branch = self._branch(conn)
            rows = [_record(row) for row in conn.execute(text("SELECT " + self.SALE_FIELDS + " FROM VENDAS WHERE CD_VENDA IN (" + placeholders + ")"), params)]
            result = []
            for sale_id in cd_vendas:
                sale_id = int(normalize_farmax_id(sale_id))
                items = [row for row in rows if normalize_farmax_id(row["cd_venda"]) == str(sale_id)]
                delivery_rows = [_record(row) for row in conn.execute(text("SELECT " + self.DELIVERY_FIELDS + " FROM ENTREGAS WHERE CD_VENDA=:id"), {"id": sale_id})]
                logs = []
                # Item changes do not require delivery logs. Read removal logs
                # only for absent deliveries (legacy tables may lack an index).
                if self.log_available and not delivery_rows:
                    query = self._bounded_log_query("CD_VENDA=:id", descending=True)
                    logs = [_record(row) for row in conn.execute(query, {"id": sale_id})]
                complete = bool(items) and len(delivery_rows) <= 1
                context = None
                try:
                    context = self._sale_context(items, branch, delivery_rows[0] if delivery_rows else None)
                    complete = complete and all(normalize_farmax_id(row["cd_filial"]) == branch for row in delivery_rows)
                except (ValueError, TypeError, KeyError):
                    complete = False
                deleted = False
                if complete and not delivery_rows and logs:
                    try:
                        latest = DeliveryLog.model_validate(logs[0])
                        sale_start = datetime.combine(_day(context["sale_date"]), _clock(context["sale_time"]))
                        deleted = (latest.action == "DELETE" and latest.log_date >= sale_start
                                   and sum(row.get("id") == latest.id for row in logs) == 1)
                    except (ValueError, TypeError):
                        pass
                for row in items or [{"cd_venda": sale_id, "status": None, "item_present": False}]:
                    try:
                        result.append(FarmaxSale.model_validate(dict(row, delivery_exists=bool(delivery_rows),
                            delivery_deleted=deleted, evidence_complete=complete, removal_evidence=logs,
                            source_context=context, snapshot_item_count=len(items))))
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
                        self._ordinary_sale(sales)
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
