from datetime import datetime, timezone
import httpx

from models.velide_delivery_models import (
    DeliveryResponse,
    GraphQLParseError,
    GraphQLPayload,
    GraphQLRequestError,
    GraphQLResponse,
    GraphQLResponseError,
    GraphQLVariables,
    MetadataInput,
    Order
)
from config import ApiConfig, TargetSystem


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
    
    def __init__(self, access_token: str, api_config: ApiConfig, target_system: TargetSystem):
        """
        Initialize the Velide API client.
        
        Args:
            access_token: Bearer token for API authentication
            api_config: Configuration object containing URL, timeout, SSL settings
        """
        self._api_config = api_config
        self._target_system = target_system
        self._headers = {
            'Content-Type': 'application/json',
            'Authorization': access_token
        }

    async def close(self):
        """Close the HTTP client connection."""
        await self._client.aclose()

    async def __aenter__(self):
        """Called when entering the 'async with' block."""
        # Create the client here. It will use the currently active event loop.
        self._client = httpx.AsyncClient(
            headers=self._headers,
            timeout=self._api_config.timeout,
            verify=self._api_config.use_ssl
        )
        return self # Return self so you can call methods on it inside the block

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Called when exiting the 'async with' block. Ensures cleanup."""
        # Cleanly close the client and its connections.
        await self._client.aclose()

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
        variables = self._build_variables(order)
        payload = GraphQLPayload(query=self.ADD_DELIVERY_MUTATION, variables=variables)
        
        # Make request and parse response
        response = await self._execute_request(payload)
        return self._parse_response(response)

    def _build_variables(
        self,
        order: Order
    ) -> GraphQLVariables:
        """
        Build GraphQL variables from sale data.
        
        Args:
            sale_data: Validated order data
            target_software: Integration software name
            use_neighbourhood: Whether to include neighbourhood
            
        Returns:
            GraphQLVariables: Validated variables for the mutation
        """
        # Calculate time offset
        offset = self._calculate_offset(order.created_at)
        
        # Build metadata
        metadata = MetadataInput(
            integrationName=self._target_system.value,
            customerName=order.customer_name,
            customerContact=order.customer_contact
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
        
        return GraphQLVariables.model_validate(variables_dict)

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
        # Get the current time as a timezone-aware object in UTC.
        # This is crucial for correctly comparing with the UTC-aware created_at_time.
        now_utc = datetime.now(timezone.utc)
        
        # Calculate the difference. This works correctly because both
        # datetime objects are timezone-aware.
        time_difference = now_utc - created_at_time
        
        # Convert the resulting timedelta object to total milliseconds.
        offset_in_milliseconds = time_difference.total_seconds() * 1000
        
        return round(offset_in_milliseconds)
    
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

    def _parse_response(self, response: httpx.Response) -> DeliveryResponse:
        """
        Parse and validate GraphQL response.
        
        Args:
            response: HTTP response from the API
            
        Returns:
            DeliveryResponse: Parsed delivery data
            
        Raises:
            GraphQLParseError: When JSON parsing fails
            GraphQLResponseError: When response structure is invalid or contains errors
        """
        # Parse JSON
        try:
            response_json = response.json()
        except Exception:
            raise GraphQLParseError(response)
        
        # Validate response structure
        try:
            graphql_response = GraphQLResponse.model_validate(response_json)
        except Exception as e:
            raise GraphQLResponseError(
                f"Unexpected response structure: {response_json}"
            ) from e
        
        # Check for errors
        if graphql_response.errors:
            raise GraphQLResponseError(
                f"GraphQL returned errors: {graphql_response.errors}"
            )
        
        if not graphql_response.data:
            raise GraphQLResponseError(f"No data in response: {response_json}")
        
        return graphql_response.data.addDeliveryFromIntegration