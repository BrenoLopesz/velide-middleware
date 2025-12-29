from datetime import datetime
from typing import Any, List, Optional, Union
from pydantic import BaseModel, Field, field_validator, ConfigDict
import httpx

# Input Models
class Order(BaseModel):
    """Input model for sale information"""
    model_config = ConfigDict(str_strip_whitespace=True)
    
    customer_name: str = Field(..., min_length=1, alias="customerName", description="Customer name.")
    address: str = Field(..., min_length=1, description="Delivery address.")
    created_at: datetime = Field(..., alias="createdAt", description="Time of sale creation.")
    
    # Optional fields
    customer_contact: Optional[str] = Field(None, alias="customerContact", description="Customer contact information.")
    reference: Optional[str] = Field(None, description="Delivery address reference.")
    address2: Optional[str] = Field(None, description="Secondary address line.")
    neighbourhood: Optional[str] = Field(None, description="Neighbourhood information.")

    # We use exclude=True so if you use model_dump() to generate payloads, 
    # this field is automatically hidden from the API.
    internal_id: str = Field(None, description="Internal ERP ID used for tracking.", exclude=True)
    ui_status_hint: Optional[Any] = Field(None, exclude=True)
    
    @field_validator('customer_name', 'address')
    @classmethod
    def validate_not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("Field cannot be empty")
        return v.strip()


class MetadataInput(BaseModel):
    """Metadata for GraphQL mutation"""
    integration_name: str = Field(..., min_length=1, alias="integrationName")
    customer_name: str = Field(..., min_length=1, alias="customerName")
    customer_contact: Optional[str] = Field(None, alias="customerContact")

class AddDeliveryVariables(BaseModel):
    """Variables specifically for adding a delivery"""
    metadata: MetadataInput 
    address: str = Field(..., min_length=1)
    offset: int = Field(0, ge=0, description="Offset in milliseconds")
    
    address2: Optional[str] = None
    neighbourhood: Optional[str] = None
    reference: Optional[str] = None

class DeleteDeliveryVariables(BaseModel):
    """Variables specifically for deleting a delivery"""
    # API expects 'deliveryId', but we want to use 'delivery_id' in Python
    delivery_id: str = Field(..., alias="deliveryId", min_length=1)

class GraphQLPayload(BaseModel):
    """Complete GraphQL request payload"""
    query: str = Field(..., min_length=1)
    # Update this line to accept the specific model OR a generic dict
    variables: Optional[Union[AddDeliveryVariables, DeleteDeliveryVariables]] = None

# Response Models
class LocationProperties(BaseModel):
    """Location properties from GraphQL response"""
    name: Optional[str] = None
    housenumber: Optional[str] = None
    street: Optional[str] = None
    neighbourhood: Optional[str] = None


class Location(BaseModel):
    """Location information from GraphQL response"""
    properties: LocationProperties

class DeliveryResponse(BaseModel):
    """Delivery data from GraphQL response"""
    id: str
    route_id: Optional[str] = Field(None, alias="routeId")
    ended_at: Optional[datetime] = Field(None, alias="endedAt")
    created_at: datetime = Field(..., alias="createdAt")
    location: Optional[Location] = None

class RouteResponse(BaseModel):
    """Route info nested inside deliveryman"""
    id: str
    deliveries: List[DeliveryResponse] = Field(default_factory=list)

class DeliverymanResponse(BaseModel):
    """Deliveryman information from GraphQL response"""
    id: str
    name: str
    route: Optional[RouteResponse] = None

class AddDeliveryData(BaseModel):
    """Data wrapper for addDeliveryFromIntegration"""
    addDeliveryFromIntegration: DeliveryResponse

class GetDeliverymenData(BaseModel):
    """Data wrapper for deliverymen query"""
    deliverymen: List[DeliverymanResponse]

class DeleteDeliveryData(BaseModel):
    deleteDelivery: bool

class GlobalSnapshotData(BaseModel):
    """
    Data wrapper for the Global Snapshot query.
    Maps to the two root keys returned by the GraphQL query.
    """
    # Unassigned deliveries
    deliveries: List[DeliveryResponse] = Field(default_factory=list)
    # Deliverymen (who might have assigned deliveries)
    deliverymen: List[DeliverymanResponse] = Field(default_factory=list)

class GraphQLResponse(BaseModel):
    """Complete GraphQL response"""
    # Add DeleteDeliveryData to the Union, or allow Dict[str, Any] as a fallback
    data: Optional[Union[
        AddDeliveryData, 
        GetDeliverymenData, 
        DeleteDeliveryData,
        GlobalSnapshotData
    ]] = None
    
    errors: Optional[list] = None
    
    @field_validator('data')
    @classmethod
    def validate_data_present(cls, v, info):
        if v is None and info.data.get('errors') is None:
            raise ValueError("Response must contain either data or errors")
        return v

# Custom Exceptions
class GraphQLError(Exception):
    """Base exception for GraphQL operations"""
    pass


class GraphQLRequestError(GraphQLError):
    """Raised when GraphQL request fails"""
    def __init__(self, status_code: int, response_text: str):
        self.status_code = status_code
        self.response_text = response_text
        super().__init__(f"GraphQL mutation failed with status code {status_code}: {response_text}")


class GraphQLResponseError(GraphQLError):
    """Raised when GraphQL response is invalid"""
    pass


class GraphQLParseError(GraphQLError):
    """Raised when response JSON parsing fails"""
    def __init__(self, response: httpx.Response):
        self.response = response
        super().__init__(f"Could not parse response as JSON. Status: {response.status_code}")

