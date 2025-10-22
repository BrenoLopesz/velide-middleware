from pydantic import BaseModel, Field, ConfigDict

# 1. Define the Base Model with CANONICAL names
# These are the attribute names you will use internally in your Python code.
class BaseLocalDeliveryman(BaseModel):
    id: str = Field(description="The local ID of the deliveryman.")
    name: str = Field(description="The local registered name of the deliveryman.")