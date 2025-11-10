from datetime import time, datetime
from enum import Enum
from typing import Literal, Optional
from pydantic import BaseModel, Field, ConfigDict

from models.base_models import BaseLocalDeliveryman

class FarmaxAction(Enum):
    INSERT = "INSERT"
    UPDATE = "UPDATE"
    DELETE = "DELETE"

class FarmaxDeliveryman(BaseLocalDeliveryman):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True) # Makes it work with SQLAlchemy
    id: int = Field(alias="CD_VENDEDOR")
    name: str = Field(alias="NOME")

class FarmaxSale(BaseModel):
    model_config = ConfigDict(from_attributes=True) # Makes it work with SQLAlchemy
    id: float = Field(alias="CD_VENDA") # Firebird uses DOUBLE PRECISION
    status: FarmaxAction = Field(alias="STATUS")

class FarmaxDelivery(BaseModel):
    model_config = ConfigDict(from_attributes=True) # Makes it work with SQLAlchemy
    # We use aliases to map the database's uppercase names 
    # to clean, Pythonic lowercase names.
    sale_id: float = Field(..., alias="CD_VENDA")
    customer_name: Optional[str] = Field(alias="NOME")
    route_start_time: Optional[time] = Field(alias="HORA_SAIDA")  # HORA_SAIDA could be NULL
    neighborhood: Optional[str] = Field(alias="BAIRRO") # BAIRRO could be NULL
    created_at: time = Field(..., alias="HORA")
    address: Optional[str] = Field(alias="TEMPENDERECO")
    reference: Optional[str] = Field(alias="TEMPREFERENCIA")

class DeliveryLog(BaseModel):
    model_config = ConfigDict(from_attributes=True) # Makes it work with SQLAlchemy
    # These aliases map to the columns in your LOG_TABLE_NAME
    id: int = Field(alias="Id")
    sale_id: float = Field(alias="CD_VENDA")
    
    # This is a huge improvement: we validate the action is only one
    # of these three values, preventing bugs from bad data.
    action: Literal["INSERT", "UPDATE", "DELETE"] = Field(alias="Action")
    log_date: datetime = Field(alias="LogDate")