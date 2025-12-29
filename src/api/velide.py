from datetime import datetime, timezone
from typing import Any, List, Optional, TypeVar
import httpx

from models.velide_delivery_models import (
    AddDeliveryVariables,
    DeleteDeliveryVariables,
    DeliveryResponse,
    DeliverymanResponse,
    GlobalSnapshotData,
    GraphQLParseError,
    GraphQLPayload,
    GraphQLRequestError,
    GraphQLResponse,
    GraphQLResponseError,
    MetadataInput,
    Order
)
from config import ApiConfig, TargetSystem
from utils.async_retry import async_retry

T = TypeVar('T')

class Velide:
    """Client for interacting with Velide delivery API via GraphQL."""
    
    # GraphQL mutation as class constant for reusability
    ADD_DELIVERY_MUTATION = """
        mutation AddDeliveryFromIntegration(
            $metadata: MetadataInput!,
            $address: String,
            $address2: String,
            $neighbourhood: String,
            $reference: String,
            $offset: Int
        ) {
            addDeliveryFromIntegration(
                metadata: $metadata
                address: $address
                address2: $address2
                neighbourhood: $neighbourhood
                reference: $reference
                offset: $offset
            ) {
                id
                routeId
                endedAt
                createdAt
                location {
                    properties {
                        name
                        housenumber
                        street
                        neighbourhood
                    }
                }
            }
        }
    """

    DELETE_DELIVERY_MUTATION = """
        mutation DeleteDelivery($deliveryId: String!) {
            deleteDelivery(deliveryId: $deliveryId)
        }
    """

    GET_DELIVERYMEN_QUERY =  """
        query {
            deliverymen {
                id
                name
            }
        }
    """

    GET_GLOBAL_SNAPSHOT_QUERY = """
        query GetGlobalSnapshot {
            deliveries {
                id
                createdAt
            }
            deliverymen {
                id
                name
                route {
                    id
                    deliveries {
                        id
                    }
                }
            }
        }
    """
    
    def __init__(self, access_token: str, api_config: ApiConfig, target_system: TargetSystem):
        """
        Initialize the Velide API client.
        
        Args:
            access_token: Bearer token for API authentication
            api_config: Configuration object containing URL, timeout, SSL settings
        """
        self._access_token = access_token
        self._api_config = api_config
        self._target_system = target_system
        self._client: Optional[httpx.AsyncClient] = None

    async def __aenter__(self):
        """Called when entering the 'async with' block."""
        # Create headers dict here
        headers = {
            'Content-Type': 'application/json',
            'Authorization': self._access_token
        }
        # Create the client here. It will use the currently active event loop.
        self._client = httpx.AsyncClient(
            headers=headers,
            timeout=self._api_config.timeout,
            verify=self._api_config.use_ssl
        )
        return self # Return self so you can call methods on it inside the block

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Called when exiting the 'async with' block. Ensures cleanup."""
        # Cleanly close the client and its connections.
        await self._client.aclose()

    @async_retry(
        operation_desc="enviar nova entrega",  # <--- Friendly Name
        max_retries=4, 
        initial_delay=2.0
    )
    async def add_delivery(
        self,
        order: Order,
    ) -> DeliveryResponse:
        """
        Add a delivery via GraphQL mutation with full type safety.
        
        Args:
            sale_info: Dictionary containing sale information
            
        Returns:
            DeliveryResponse: The created delivery information
            
        Raises:
            GraphQLRequestError: When the HTTP request fails
            GraphQLParseError: When response parsing fails
            GraphQLResponseError: When response structure is invalid
            ValidationError: When input data validation fails
        """
        # Build request components
        variables = self._build_variables_to_add_delivery(order)
        payload = GraphQLPayload(query=self.ADD_DELIVERY_MUTATION, variables=variables)
        
        # Make request and parse response
        response = await self._execute_request(payload)
        
        # Use the new generic parser
        return self._parse_response(response, data_key="addDeliveryFromIntegration")
    
    @async_retry(
        operation_desc="deletar entrega",
        max_retries=3
    )
    async def delete_delivery(self, delivery_id: str) -> bool:
        """
        Deletes a delivery by ID.
        
        Args:
            delivery_id: The unique ID of the delivery to remove.
            
        Returns:
            bool: True if deletion was successful.
        """
        variables = DeleteDeliveryVariables(deliveryId=delivery_id)
        payload = GraphQLPayload(
            query=self.DELETE_DELIVERY_MUTATION, 
            variables=variables
        )

        response = await self._execute_request(payload)
        
        # Return a boolean for success.
        return self._parse_response(response, data_key="deleteDelivery")

    @async_retry(
        operation_desc="buscar entregadores",
        max_retries=3
    )
    async def get_deliverymen(self) -> List[DeliverymanResponse]:
        """
        Retrieves the list of deliverymen.
        
        Returns:
            DeliverymanResponse: Parsed deliveryman data
        """
        payload = GraphQLPayload(query=self.GET_DELIVERYMEN_QUERY)

        response = await self._execute_request(payload)
        
        # Use the new generic parser with the correct key (and fix the bug)
        return self._parse_response(response, data_key="deliverymen")

    @async_retry(
        operation_desc="buscar snapshot global",
        max_retries=3
    )
    async def get_active_deliveries_snapshot(self) -> dict:
        """
        Fetches ALL unassigned deliveries and ALL active routes.
        Returns a simplified dictionary mapping Delivery IDs to their current status.
        
        Returns:
            dict: { "delivery_id": "STATUS" } 
            e.g. { "abc-123": "PENDING", "xyz-789": "ROUTED" }
        """
        payload = GraphQLPayload(query=self.GET_GLOBAL_SNAPSHOT_QUERY)
        
        response = await self._execute_request(payload)
        
        # Pass None to get the full data structure containing both keys
        raw_data = self._parse_response(response, data_key=None)
        
        # Flatten the data for easier processing
        return self._flatten_snapshot(raw_data)

    def _build_variables_to_add_delivery(
        self, 
        order: Order
    ) -> AddDeliveryVariables:
        """
        Build GraphQL variables from order data.
        
        Args:
            order: Validated order
            
        Returns:
            GraphQLVariables: Validated variables for the mutation
        """
        # Calculate time offset
        offset = self._calculate_offset(order.created_at)
        
        # Build metadata
        metadata = MetadataInput(
            integrationName=self._target_system.value,
            customerName=order.customer_name,
            customerContact=getattr(order, "customer_contact", None)
        )

        # Build variables dictionary with required fields
        variables_dict = {
            'metadata': metadata.model_dump(exclude_none=True, by_alias=True),
            'address': order.address,
            'offset': offset if offset > 60000 else 0,
        }
        
        # Add optional fields
        if order.reference:
            variables_dict['reference'] = order.reference
        if order.address2:
            variables_dict['address2'] = order.address2
        if self._api_config.use_neighbourhood and order.neighbourhood:
            variables_dict['neighbourhood'] = order.neighbourhood
        
        return AddDeliveryVariables.model_validate(variables_dict)

    def _calculate_offset(self, created_at_time: datetime) -> int:
        """
        Calculate time offset in milliseconds from sale creation time.
        
        It assumes the input 'created_at_time' is a timezone-aware datetime object,
        as parsed by Pydantic from an ISO 8601 string with timezone info (e.g., "Z").
        
        Args:
            created_at_time: Time when the sale was created (timezone-aware).
            
        Returns:
            int: Offset in milliseconds.
        """
        now_utc = datetime.now(timezone.utc)
        
        # Fix Naive Datetime if necessary
        # The Pydantic model uses datetime.combine(), which returns a naive datetime.
        if created_at_time.tzinfo is None:
            # .astimezone() without args assumes the datetime is in the 
            # local system timezone and makes it aware.
            created_at_time = created_at_time.astimezone()
        
        # 3. Calculate difference (Now both are Aware)
        time_difference = now_utc - created_at_time
        
        # 4. Convert to milliseconds
        offset_in_milliseconds = time_difference.total_seconds() * 1000
        
        return round(offset_in_milliseconds)    
    
    def _flatten_snapshot(self, data: GlobalSnapshotData) -> dict:
        """
        Helper to convert the strictly typed Snapshot data into a simple Status Map.
        """
        snapshot_map = {}
        
        # 1. Process Unassigned Deliveries (Status: PENDING)
        # Pydantic ensures 'data.deliveries' is a list (never None) due to default_factory
        for item in data.deliveries:
            snapshot_map[item.id] = "PENDING"

        # 2. Process Assigned Deliveries (Status: ROUTED)
        for dm in data.deliverymen:
            # Pydantic ensures dm.route is either a SnapshotRoute object or None
            if dm.route:
                for item in dm.route.deliveries:
                    snapshot_map[item.id] = "ROUTED"

        return snapshot_map
    
    async def _execute_request(self, payload: GraphQLPayload) -> httpx.Response:
        """
        Execute GraphQL request.
        
        Args:
            payload: GraphQL payload with query and variables
            
        Returns:
            httpx.Response: HTTP response object
            
        Raises:
            GraphQLRequestError: When HTTP request fails
        """
        response = await self._client.post(
            self._api_config.velide_server,
            json=payload.model_dump(mode='json', by_alias=True)
        )
        
        if response.status_code != 200:
            raise GraphQLRequestError(response.status_code, response.text)
        
        return response
    
    def _parse_response(self, response: httpx.Response, data_key: Optional[str] = None) -> T:
        """
        Generically parse and validate a GraphQL response, extracting data from a specific key.
        
        Args:
            response: HTTP response from the API
            data_key: The key within the 'data' object to extract (e.g., 'addDeliveryFromIntegration')
            
        Returns:
            T: The extracted data (e.g., a DeliveryResponse or List[DeliverymanResponse])
            
        Raises:
            GraphQLParseError: When JSON parsing fails
            GraphQLResponseError: When response structure is invalid, contains errors,
                                 or the data_key is missing.
        """
        # 1. Parse JSON
        try:
            response_json = response.json()
        except Exception:
            raise GraphQLParseError(response)
        
        # 2. Validate response structure
        try:
            graphql_response = GraphQLResponse.model_validate(response_json)
        except Exception as e:
            raise GraphQLResponseError(
                f"Unexpected response structure: {response_json}"
            ) from e
        
        # 3. Check for GraphQL errors
        if graphql_response.errors:
            raise GraphQLResponseError(
                f"GraphQL returned errors: {graphql_response.errors}"
            )
        
        # 4. Check for 'data' field
        if not graphql_response.data:
            raise GraphQLResponseError(f"No 'data' in response: {response_json}")
        
        # If no specific key is requested, return the whole data object/dict
        if data_key is None:
            return graphql_response.data

        # 5. Extract specific data using the key
        try:
            # Use getattr to dynamically access the attribute
            data = getattr(graphql_response.data, data_key)
            if data is None:
                # Handle cases where the key exists but is null (and not expected)
                raise GraphQLResponseError(
                    f"Data key '{data_key}' is null in response: {response_json}"
                )
            return data
        except AttributeError:
            # This catches if graphql_response.data doesn't have the attribute `data_key`
            raise GraphQLResponseError(
                f"Expected data key '{data_key}' not found in response data: {response_json}"
            )