import logging
from typing import List, Set, Callable, Optional, Any

# Assuming these models exist based on previous context
from models.farmax_models import FarmaxDelivery, DeliveryLog, FarmaxAction
from models.velide_delivery_models import Order

class FarmaxMapper:
    """
    A pure utility class responsible for:
    1. Transforming Farmax domain models into System (Velide) models.
    2. Filtering logic to determine which logs constitute a 'New Delivery'.
    
    This class is stateless and contains no infrastructure or threading logic.
    """

    _logger = logging.getLogger(__name__)

    @staticmethod
    def to_order(delivery: FarmaxDelivery) -> Order:
        """
        Normalizes a FarmaxDelivery object into the generic Order model.
        
        Args:
            delivery (FarmaxDelivery): The raw data from the SQL query.
            
        Returns:
            Order: The normalized order ready for the API or UI.
        """
        # distinct conversion logic allows us to handle edge cases
        # (e.g., formatting phone numbers, cleaning strings) here centrally.
        
        return Order(
            customerName=FarmaxMapper._safe_str(delivery.customer_name),
            customerContact=FarmaxMapper._safe_str(getattr(delivery, "customer_contact", None)),
            
            # Address Block
            address=FarmaxMapper._safe_str(delivery.address),
            neighborhood=FarmaxMapper._safe_str(getattr(delivery, "neighborhood", None)),
            reference=FarmaxMapper._safe_str(getattr(delivery, "reference", None)),
            
            # Metadata
            createdAt=delivery.created_at,
            internal_id=str(delivery.sale_id)
        )

    @staticmethod
    def filter_new_insert_ids(
        logs: List[DeliveryLog], 
        is_tracked_check: Callable[[float], bool]
    ) -> Set[float]:
        """
        Analyzes a batch of logs to find Sales IDs that represent NEW orders
        that are not currently being tracked.

        Args:
            logs (List[DeliveryLog]): The batch of logs from the database.
            is_tracked_check (Callable[[float], bool]): A function (predicate) 
                that returns True if an ID is already known to the system.

        Returns:
            Set[float]: A unique set of Sale IDs that need to be fetched.
        """
        relevant_ids: Set[float] = set()

        for log in logs:
            # 1. Validate Action Type
            # Using getattr defaults to empty string to prevent crashes on malformed objects
            action = str(getattr(log, 'action', '')).upper()
            
            # We only care about INSERTs. Updates (e.g., status changes) are handled elsewhere.
            if action != FarmaxAction.INSERT.value:
                continue

            # 2. Validate ID
            sale_id = getattr(log, 'sale_id', None)
            if not sale_id:
                continue

            # 3. Check Persistence (via dependency injection)
            if not is_tracked_check(sale_id):
                relevant_ids.add(sale_id)

        return relevant_ids

    @staticmethod
    def _safe_str(value: Any) -> Optional[str]:
        """Helper to ensure None is preserved as None, but strings are stripped."""
        if value is None:
            return None
        return str(value).strip()