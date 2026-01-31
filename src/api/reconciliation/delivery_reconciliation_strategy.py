from datetime import datetime, timedelta, timezone
from typing import Optional, List, Tuple, TYPE_CHECKING
import logging

from models.velide_delivery_models import (
    DeliveryResponse,
    Order,
    GlobalSnapshotData,
    MetadataResponse
)
from config import ReconciliationConfig

if TYPE_CHECKING:
    from api.velide import Velide


class DeliveryReconciliationStrategy:
    """Strategy for reconciling delivery operations during retries.

    This class encapsulates the business logic for determining if a delivery
    was successfully created on the remote Velide system despite a client-side
    timeout or error. It queries the API and performs fuzzy matching on
    metadata to find existing records.

    Attributes:
        _velide_client: The API client instance.
        _config: Configuration for reconciliation parameters.
        _logger: Logger instance for this class.
    """

    def __init__(
        self,
        velide_client: "Velide",
        config: ReconciliationConfig
    ):
        """Initializes the reconciliation strategy.

        Args:
            velide_client: An instance of the Velide API client.
            config: Configuration object containing retry windows and delays.
        """
        self._velide_client = velide_client
        self._config = config
        self._logger = logging.getLogger(__name__)

    @property
    def delay_seconds(self) -> float:
        """float: The delay in seconds to wait before attempting reconciliation."""
        return self._config.retry_reconciliation_delay_seconds

    async def check_exists(self, *args, **kwargs) -> Optional[DeliveryResponse]:
        """Orchestrates the check for an existing delivery.

        Expects an 'Order' object either as the first positional argument
        or as a keyword argument named 'order'.

        Args:
            *args: Variable length argument list. First arg should be Order.
            **kwargs: Arbitrary keyword arguments. Should contain 'order'.

        Returns:
            Optional[DeliveryResponse]: The matching delivery if found, else None.
        """
        # Extract order from args or kwargs
        order: Optional[Order] = None
        if args and isinstance(args[0], Order):
            order = args[0]
        elif 'order' in kwargs:
            order = kwargs['order']

        if order is None:
            self._logger.warning(
                "Não foi possível reconciliar: nenhum pedido encontrado nos argumentos."
            )
            return None

        return await self._find_delivery_by_metadata(order)

    async def _find_delivery_by_metadata(
        self,
        order: Order
    ) -> Optional[DeliveryResponse]:
        """Queries the API and filters for a matching delivery.

        Fetches the full global snapshot of active deliveries from Velide and
        delegates to `_find_best_match` to apply business rules (time window,
        name matching, address matching).

        Args:
            order: The original order that failed to confirm.

        Returns:
            Optional[DeliveryResponse]: The matching delivery if found, else None.
        """
        try:
            # Requires Velide client to expose the RAW data method
            snapshot_data: GlobalSnapshotData = await self._velide_client.get_full_global_snapshot()

            if not snapshot_data or not snapshot_data.deliveries:
                return None

            match = self._find_best_match(snapshot_data.deliveries, order)

            if match:
                self._logger.info(
                    f"Reconciliação encontrou entrega existente: {match.id}"
                )
            
            return match

        except Exception:
            self._logger.exception("Falha na consulta de reconciliação.")
            return None

    def _find_best_match(
        self,
        deliveries: List[DeliveryResponse],
        order: Order
    ) -> Optional[DeliveryResponse]:
        """Filters a list of deliveries to find the best candidate for the order.

        Applies the following filters:
        1. Metadata existence.
        2. Customer name (case-insensitive).
        3. Creation time (must be within the configured time window).
        4. Address matching (fuzzy/substring check).

        If multiple candidates pass all filters, the most recently created
        delivery is returned to handle potential race conditions or duplicates.

        Args:
            deliveries: List of active deliveries from the API.
            order: The order to match against.

        Returns:
            Optional[DeliveryResponse]: The best matching delivery, or None.
        """
        # Calculate the lookback cutoff time (UTC)
        cutoff_time = datetime.now(timezone.utc) - timedelta(
            seconds=self._config.retry_reconciliation_time_window_seconds
        )
        
        candidates: List[Tuple[DeliveryResponse, datetime]] = []

        for delivery in deliveries:
            # 1. Check Metadata Existence
            if not delivery.metadata:
                continue

            # 2. Check Customer Name (Case-Insensitive)
            stored_name = delivery.metadata.customer_name
            if not stored_name or stored_name.lower() != order.customer_name.lower():
                continue

            # 3. Check Time Window (Timezone Safe)
            created_at = delivery.created_at
            # Force UTC if naive to prevent runtime crash
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
            
            if created_at < cutoff_time:
                continue

            # 4. Check Address Match using Strategy Logic
            if self._address_matches(delivery.metadata, order.address):
                candidates.append((delivery, created_at))

        if not candidates:
            return None

        # Sort by creation time descending (newest first)
        candidates.sort(key=lambda x: x[1], reverse=True)
        
        return candidates[0][0]

    def _address_matches(
        self,
        metadata: MetadataResponse,
        input_address: str
    ) -> bool:
        """Compares input address against stored metadata address.

        Uses raw string comparison rather than geocoded properties to ensure
        reliable matching of the exact input sent by the integration.

        Args:
            metadata: The metadata from a candidate delivery.
            input_address: The raw address string from the order.

        Returns:
            bool: True if the addresses match, False otherwise.
        """
        if not metadata.address:
            return False

        # Normalize strings
        stored_addr = metadata.address.strip().lower()
        search_addr = input_address.strip().lower()

        if not stored_addr or not search_addr:
            return False

        # Exact match
        if stored_addr == search_addr:
            return True

        # Safety: Prevent short strings (e.g. "10") from matching inside long ones
        if len(search_addr) < 5:
            return False

        # Substring match (Bidirectional for robustness)
        return search_addr in stored_addr or stored_addr in search_addr