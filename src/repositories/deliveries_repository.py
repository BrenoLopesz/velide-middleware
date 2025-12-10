from typing import Dict, Optional
from models.velide_delivery_models import Order

class DeliveryRepository:
    def __init__(self):
        # Primary storage: Internal ID -> Order
        self._orders: Dict[str, Order] = {}
        # Index: External ID -> Internal ID (for fast lookup via WebSockets)
        self._external_to_internal: Dict[str, str] = {}

    def add(self, order: Order):
        self._orders[order.internal_id] = order

    def get_by_internal(self, internal_id: str) -> Optional[Order]:
        return self._orders.get(internal_id)

    def get_by_external(self, external_id: str) -> Optional[Order]:
        internal_id = self._external_to_internal.get(external_id)
        if internal_id:
            return self._orders.get(internal_id)
        return None

    def remove(self, internal_id: str):
        order = self._orders.pop(internal_id, None)
        # Clean up the reverse index if it exists
        if order:
            # We need to find the key for this value or store external_id on the order object
            # Assuming Order has an 'external_id' attribute or we search:
            keys_to_remove = [k for k, v in self._external_to_internal.items() if v == internal_id]
            for k in keys_to_remove:
                del self._external_to_internal[k]

    def link_ids(self, internal_id: str, external_id: str):
        """Maps the Velide ID to the ERP ID."""
        if internal_id in self._orders:
            self._external_to_internal[external_id] = internal_id