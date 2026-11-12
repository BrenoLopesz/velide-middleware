"""Direct native GraphQL transport. Acceptance and delivery creation are separate."""
from hashlib import sha256

import httpx
import pytz
from pydantic import BaseModel

from models.velide_delivery_models import DeliverymanResponse


class NativeApiError(Exception):
    def __init__(self, message, code="REMOTE_ERROR", retryable=False):
        super().__init__(message)
        self.code, self.retryable = code, retryable


class NativeRequest(BaseModel):
    input: dict


STATE_FIELDS = """externalOrderId found revision jobId importStatus deliveryId deliveryStatus
    deleted cancellation retainedOutcome code retryable error
    execution { id courierId status startedAt completedAt abortedAt }
    source { connectionId externalOrderId customerName customerContact sourceContext }
    request { rawAddress rawComplemento rawNeighbourhood rawReference createdAtIntent }"""


class NativeVelide:
    def __init__(self, access_token, api_config, target_system, reconciliation_config=None):
        self.config, self.target = api_config, target_system
        self.native = api_config.native
        self._token = access_token[7:] if access_token.startswith("Bearer ") else access_token
        self._client = None

    async def __aenter__(self):
        self._client = httpx.AsyncClient(timeout=self.config.timeout, verify=True,
            headers={"Authorization": "Bearer " + self._token, "X-Merchant-ID": self.native.merchant_id})
        return self

    async def __aexit__(self, *args):
        await self._client.aclose()

    async def _request(self, query, variables, field):
        response = await self._client.post(self.config.velide_server, json={"query": query, "variables": variables})
        if response.status_code != 200:
            raise NativeApiError("Native API HTTP {}".format(response.status_code),
                                 "HTTP_{}".format(response.status_code), response.status_code in (401, 408, 429) or response.status_code >= 500)
        try:
            body = response.json()
            if body.get("errors"):
                error = body["errors"][0]
                code = error.get("extensions", {}).get("code", "GRAPHQL_ERROR")
                raise NativeApiError(error.get("message", "GraphQL error"), code, code in ("UNAUTHENTICATED", "TOKEN_EXPIRED"))
            result = body["data"][field]
        except (ValueError, KeyError, TypeError) as error:
            raise NativeApiError("Invalid native API response", "INVALID_RESPONSE", True) from error
        if isinstance(result, dict) and result.get("success") is False:
            raise NativeApiError(result.get("error") or "Native operation rejected",
                                 result.get("code") or "REJECTED", result.get("retryable") is True)
        return result

    async def validate_scope(self):
        merchants = await self._request("query { myMerchants { id permissions settings { timezone } } }", {}, "myMerchants")
        selected = next((m for m in merchants if m["id"] == self.native.merchant_id), None)
        required = {"integration.import", "integration.read", "integration.cancel", "integration.retry", "courier.read"}
        if selected is None or not required.issubset(set(selected.get("permissions", []))):
            raise NativeApiError("Select an authorized merchant with the required integration permissions", "MERCHANT_ACCESS")
        if selected.get("settings", {}).get("timezone") != self.native.timezone:
            raise NativeApiError("Configured source timezone differs from merchant timezone", "TIMEZONE_MISMATCH")
        # This also checks the principal's source binding, even with no orders yet.
        await self.order_states([])
        return selected

    def _build_variables_to_add_delivery(self, order):
        if order.request_variables is not None:
            return NativeRequest.model_validate(order.request_variables)
        created = order.created_at
        if created.tzinfo is None:
            created = pytz.timezone(self.native.timezone).localize(created, is_dst=None)
        key = "integration:{}:{}".format(self.native.connection_id, sha256(order.internal_id.encode("utf-8")).hexdigest())
        return NativeRequest(input={
            "merchantId": self.native.merchant_id, "idempotencyKey": key,
            "rawAddress": order.address, "rawComplemento": order.address2,
            "rawNeighbourhood": order.neighbourhood, "rawReference": order.reference,
            "createdAtIntent": created.isoformat(), "integration": {
                "connectionId": self.native.connection_id, "externalOrderId": order.internal_id,
                "customerName": order.customer_name, "customerContact": order.customer_contact,
                "sourceContext": order.source_context,
            },
        })

    async def submit_import(self, request):
        result = await self._request("""mutation($input:ImportDeliveryInput!) {
            importIntegrationOrder(input:$input) { success jobId code retryable error }
        }""", request, "importIntegrationOrder")
        if not result.get("jobId"):
            raise NativeApiError("Accepted import has no job ID", "INVALID_RESPONSE", True)
        return result

    async def order_states(self, references):
        result = await self._request("""query($merchantId:ID!,$connectionId:ID!,$refs:[String!]!) {
            integrationOrderStates(merchantId:$merchantId,connectionId:$connectionId,externalOrderIds:$refs) {
            """ + STATE_FIELDS + " } }", {"merchantId": self.native.merchant_id,
                                           "connectionId": self.native.connection_id, "refs": references}, "integrationOrderStates")
        if not isinstance(result, list) or sorted(item.get("externalOrderId") for item in result) != sorted(references):
            raise NativeApiError("Incomplete source-state response", "INVALID_RESPONSE", True)
        return result

    async def list_orders(self, after=""):
        return await self._request("""query($merchantId:ID!,$connectionId:ID!,$after:String) {
            integrationOrders(merchantId:$merchantId,connectionId:$connectionId,after:$after,limit:100) {
            """ + STATE_FIELDS + " } }", {"merchantId": self.native.merchant_id,
                                           "connectionId": self.native.connection_id, "after": after}, "integrationOrders")

    async def cancel_order(self, external_order_id):
        return await self._order_mutation("cancelIntegrationOrder", external_order_id)

    async def retry_order(self, external_order_id):
        return await self._order_mutation("retryIntegrationOrder", external_order_id)

    async def _order_mutation(self, field, external_order_id):
        return await self._request("mutation($merchantId:ID!,$connectionId:ID!,$externalOrderId:String!) { "
            + field + "(merchantId:$merchantId,connectionId:$connectionId,externalOrderId:$externalOrderId)"
            + " { success jobId code retryable error } }", {"merchantId": self.native.merchant_id,
                "connectionId": self.native.connection_id, "externalOrderId": external_order_id}, field)

    async def get_deliverymen(self):
        result = await self._request("query($merchantId:ID!) { couriers(merchantId:$merchantId) { success error couriers { id name } } }",
                                     {"merchantId": self.native.merchant_id}, "couriers")
        return [DeliverymanResponse.model_validate(row) for row in result["couriers"]]
