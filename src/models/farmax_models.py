from datetime import date, time, datetime
from enum import Enum
from typing import Literal, Optional
from pydantic import BaseModel, Field, ConfigDict

from models.base_models import BaseLocalDeliveryman


class FarmaxAction(Enum):
    INSERT = "INSERT"
    UPDATE = "UPDATE"
    DELETE = "DELETE"


class FarmaxDeliveryman(BaseLocalDeliveryman):
    model_config = ConfigDict(populate_by_name=True, coerce_numbers_to_str=True)
    id: str = Field(alias="cd_vendedor")
    name: str = Field(alias="nome")


class FarmaxSale(BaseModel):
    id: float = Field(alias="cd_venda")  # Firebird uses DOUBLE PRECISION
    status: str = Field(alias="status")


class FarmaxDelivery(BaseModel):
    # We use aliases to map the database's uppercase names
    # to clean, Pythonic lowercase names.
    sale_id: float = Field(..., alias="cd_venda")
    customer_name: str = Field(..., alias="nome")
    customer_contact: Optional[str] = Field(default=None, alias="fone")
    route_start_time: Optional[time] = Field(
        default=None, alias="hora_saida"
    )  # HORA_SAIDA could be NULL
    neighborhood: Optional[str] = Field(
        default=None, alias="bairro"
    )  # BAIRRO could be NULL
    address: str = Field(..., alias="tempendereco")
    reference: Optional[str] = Field(default=None, alias="tempreferencia")
    delivery_date: date = Field(..., alias="data")
    delivery_time: time = Field(..., alias="hora")

    @property
    def created_at(self) -> datetime:
        return datetime.combine(self.delivery_date, self.delivery_time)


def parse_flexible_timestamp(v: Any) -> Any:
    """
    Tries to parse Firebird format first. 
    If it fails, returns raw value for Pydantic standard validation.
    """
    if isinstance(v, str):
        try:
            # Try Firebird format: DD.MM.YYYY HH:MM:SS.ffffff
            return datetime.strptime(v, "%d.%m.%Y %H:%M:%S.%f")
        except ValueError:
            # If format doesn't match, ignore error and return original string.
            # Pydantic's internal validator will take over from here.
            pass
    return v

# Define a reusable type that applies this logic before validation
FirebirdOrIsoDatetime = Annotated[datetime, BeforeValidator(parse_flexible_timestamp)]

class DeliveryLog(BaseModel):
    # These aliases map to the columns in your LOG_TABLE_NAME
    id: int = Field(alias="id")
    sale_id: float = Field(alias="cd_venda")

    # This is a huge improvement: we validate the action is only one
    # of these three values, preventing bugs from bad data.
    action: Literal["INSERT", "UPDATE", "DELETE"] = Field(alias="action")
    log_date: FirebirdOrIsoDatetime = Field(alias="logdate")
