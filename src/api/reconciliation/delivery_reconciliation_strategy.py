from datetime import datetime, timedelta
from typing import Any, Optional, TYPE_CHECKING
import logging

from models.velide_delivery_models import DeliveryResponse, Order
from config import ReconciliationConfig

if TYPE_CHECKING:
    from api.velide import Velide


class DeliveryReconciliationStrategy:
    """
    Reconciliation strategy for delivery operations.

    Checks if a delivery was actually created on Velide after a timeout
    by querying the API using metadata (customer name, address, time window).

    This allows the retry logic to remain agnostic of Velide-specific
    reconciliation details.
    """

    def __init__(
        self,
        velide_client: "Velide",
        config: ReconciliationConfig
    ):
        self._velide_client = velide_client
        self._config = config
        self._logger = logging.getLogger(__name__)

    @property
    def delay_seconds(self) -> float:
        """Delay before reconciliation check to allow server processing."""
        return self._config.retry_reconciliation_delay_seconds

    async def check_exists(self, *args, **kwargs) -> Optional[DeliveryResponse]:
        """
        Check if a delivery already exists on Velide.

        Expects the first positional argument or 'order' keyword argument
        to be an Order object.

        Args:
            *args: Should contain Order as first element
            **kwargs: May contain 'order' key

        Returns:
            DeliveryResponse if found, None otherwise
        """
        # Extract order from args or kwargs
        order: Optional[Order] = None
        if args and isinstance(args[0], Order):
            order = args[0]
        elif 'order' in kwargs:
            order = kwargs['order']

        if order is None:
            self._logger.warning(
                "Não foi possível reconciliar: nenhum pedido encontrado."
            )
            return None

        return await self._find_delivery_by_metadata(order)

    async def _find_delivery_by_metadata(
        self,
        order: Order
    ) -> Optional[DeliveryResponse]:
        """
        Query Velide API for deliveries matching the order's metadata.

        Uses customer name and time window to find potentially matching deliveries.
        Address matching is performed to confirm the delivery matches.
        """
        try:
            # Use the Velide client to find delivery by metadata
            # The client should already be in an async context when this is called
            result = await self._velide_client.find_delivery_by_metadata(
                customer_name=order.customer_name,
                address=order.address,
                time_window_seconds=self._config.retry_reconciliation_time_window_seconds
            )

            if result is not None:
                self._logger.info(
                    f"Reconciliação encontrou entrega existente: {getattr(result, 'id', 'ID desconhecido')}"
                )

            return result

        except Exception:
            self._logger.exception("Falha na consulta de reconciliação")
            return None