from datetime import datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, field_validator

class CdsOrder(BaseModel):
    """
    A Pydantic model to validate a customer order (Pedido).
    """
    nome_cliente: str
    endereco: str
    horario_pedido: datetime
    contato_cliente: str
    complemento: Optional[str] = None
    referencia: Optional[str] = None
    valor_pedido: Decimal

    @field_validator('valor_pedido', mode='before')
    @classmethod
    def parse_valor_pedido(cls, v):
        """
        This validator runs before standard validation (mode='before').
        It takes the raw string input, replaces the comma with a
        period, and returns it for Pydantic to parse as a Decimal.
        """
        if isinstance(v, str):
            return v.replace(',', '.', 1)
        return v
