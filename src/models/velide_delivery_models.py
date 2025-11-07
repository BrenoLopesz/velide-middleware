from datetime import datetime
from typing import List, Optional, Union
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
    customer_contact: Optional[str] = Field(..., alias="customerContact")


class GraphQLVariables(BaseModel):
    """Variables for GraphQL mutation"""
    metadata: Optional[MetadataInput]
    address: str = Field(..., min_length=1)
    offset: int = Field(0, ge=0, description="Offset in milliseconds")
    
    # Optional fields
    address2: Optional[str] = None
    neighbourhood: Optional[str] = None
    reference: Optional[str] = None


class GraphQLPayload(BaseModel):
    """Complete GraphQL request payload"""
    query: str = Field(..., min_length=1)
    variables: Optional[GraphQLVariables] = None


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

class DeliverymanResponse(BaseModel):
    """Deliveryman information from GraphQL response"""
    id: str
    name: str

class DeliveryResponse(BaseModel):
    """Delivery data from GraphQL response"""
    id: str
    route_id: Optional[str] = Field(..., alias="routeId")
    ended_at: Optional[datetime] = Field(..., alias="endedAt")
    created_at: datetime = Field(..., alias="createdAt")
    location: Optional[Location] = None


class AddDeliveryData(BaseModel):
    """Data wrapper for addDeliveryFromIntegration"""
    addDeliveryFromIntegration: DeliveryResponse

class GetDeliverymenData(BaseModel):
    """Data wrapper for deliverymen query"""
    deliverymen: List[DeliverymanResponse]

class GraphQLResponse(BaseModel):
    """Complete GraphQL response"""
    data: Optional[Union[AddDeliveryData, GetDeliverymenData]] = None
    errors: Optional[list] = None
    
    @field_validator('data')
    @classmethod
    def validate_data_present(cls, v, info):
        # This validator remains correct
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

