from datetime import datetime
from enum import Enum
from typing import List, Optional
from pydantic import BaseModel, ConfigDict, Field, field_validator

from models.velide_delivery_models import DeliveryResponse, DeliverymanResponse

class ActionType(Enum):
    ADD_DELIVERY = "ADD_DELIVERY"
    DELETE_DELIVERY = "DELETE_DELIVERY" 
    EDIT_DELIVERY_LOCATION = "EDIT_DELIVERY_LOCATION" 
    END_ROUTE = "END_ROUTE" 
    START_ROUTE = "START_ROUTE" 

class Route(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    id: str
    deliveryman: DeliverymanResponse = Field(description="Deliveryman responsible for this route.")
    started_at: datetime = Field(description="When the route has started.", alias="startedAt")
    end_at: datetime = Field(description="When the route has ended, if done yet.", alias="endAt")
    deliveries: List[DeliveryResponse] = Field(description="The deliveries associated to this route.")

class LatestAction(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    action_type: ActionType = Field(description="The kind of action taken by an user.", alias="actionType")
    timestamp: datetime = Field(description="When the event was acknowledge by the serer.")
    deliveryman: Optional[DeliverymanResponse] = Field(description="The deliveryman related to the action, if any.")
    delivery: Optional[DeliveryResponse] = Field(description="The delivery related to the action, if any.")
    offset: int = Field(
        default=0,  # Handles missing field
        description="Offset related to the 'timestemp', that defines when the action was really taken."
    )

    @field_validator('offset', mode='before')
    @classmethod
    def none_to_zero(cls, v):
        return 0 if v is None else v