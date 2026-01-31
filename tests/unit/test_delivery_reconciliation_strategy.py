"""
Unit tests for the DeliveryReconciliationStrategy.

These tests verify that the DeliveryReconciliationStrategy correctly implements
the reconciliation logic for delivery operations, checking if deliveries that
appeared to fail (due to timeout) actually succeeded on the server.
"""
import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

from api.reconciliation.delivery_reconciliation_strategy import (
    DeliveryReconciliationStrategy
)
from models.velide_delivery_models import (
    DeliveryResponse, Order, Location, LocationProperties, 
    MetadataResponse, GlobalSnapshotData
)
from config import ReconciliationConfig


# --- Helpers ---

def create_test_order(customer_name="John Doe", address="123 Main St"):
    """Helper to create a test order with required fields."""
    return Order(
        customerName=customer_name,
        address=address,
        createdAt=datetime.now(timezone.utc),
        internal_id="TEST-001",
        customerContact=None,
        reference=None,
        address2=None,
        neighbourhood=None,
        ui_status_hint=None
    )


def create_test_delivery(
    delivery_id="velide-123",
    street="123 Main St",
    housenumber="",
    customer_name="John Doe"
):
    """
    Helper to create a test delivery response.
    CRITICAL: Maps 'street' to 'metadata.address' for the new strategy logic.
    """
    return DeliveryResponse(
        id=delivery_id,
        createdAt=datetime.now(timezone.utc),
        routeId=None,
        endedAt=None,
        location=Location(
            properties=LocationProperties(
                street=street,
                housenumber=housenumber,
                neighbourhood=None,
                name=None
            )
        ),
        metadata=MetadataResponse(
            customerName=customer_name,
            integrationName="TestSystem",
            # The strategy matches against THIS field now:
            address=street 
        )
    )

def create_snapshot(deliveries):
    """Helper to wrap list of deliveries in a snapshot."""
    return GlobalSnapshotData(
        deliveries=deliveries,
        deliverymen=[]
    )

# --- Tests ---

class TestDeliveryReconciliationStrategyCheckExists:
    """Test the check_exists method of DeliveryReconciliationStrategy."""

    @pytest.fixture
    def mock_velide(self):
        """Create a mock Velide client."""
        mock = MagicMock()
        # Mock the NEW method used by strategy
        mock.get_full_global_snapshot = AsyncMock()
        return mock

    @pytest.fixture
    def config(self):
        """Create test configuration."""
        return ReconciliationConfig(
            retry_reconciliation_enabled=True,
            retry_reconciliation_delay_seconds=0.1,
            retry_reconciliation_time_window_seconds=300.0
        )

    @pytest.fixture
    def order(self):
        """Create a test order."""
        return create_test_order()

    @pytest.fixture
    def existing_delivery(self):
        """Create an existing delivery response."""
        return create_test_delivery()

    @pytest.mark.asyncio
    async def test_check_exists_finds_matching_delivery(self, mock_velide, config, order, existing_delivery):
        """
        Verify that check_exists finds a delivery matching the customer name and address.
        """
        # Arrange
        mock_velide.get_full_global_snapshot.return_value = create_snapshot([existing_delivery])
        strategy = DeliveryReconciliationStrategy(mock_velide, config)

        # Act
        result = await strategy.check_exists(order)

        # Assert
        assert result is not None
        assert result.id == "velide-123"
        mock_velide.get_full_global_snapshot.assert_called_once()

    @pytest.mark.asyncio
    async def test_check_exists_returns_none_when_no_match(self, mock_velide, config, order):
        """
        Verify that check_exists returns None when no matching delivery exists.
        """
        # Arrange - Empty snapshot
        mock_velide.get_full_global_snapshot.return_value = create_snapshot([])
        strategy = DeliveryReconciliationStrategy(mock_velide, config)

        # Act
        result = await strategy.check_exists(order)

        # Assert
        assert result is None
        mock_velide.get_full_global_snapshot.assert_called_once()

    @pytest.mark.asyncio
    async def test_check_exists_handles_exception(self, mock_velide, config, order):
        """
        Verify that check_exists returns None on API error (swallows error to allow retry).
        """
        # Arrange
        mock_velide.get_full_global_snapshot.side_effect = Exception("API Error")
        strategy = DeliveryReconciliationStrategy(mock_velide, config)

        # Act
        result = await strategy.check_exists(order)

        # Assert
        assert result is None

    @pytest.mark.asyncio
    async def test_extracts_order_from_args(self, mock_velide, config, order, existing_delivery):
        """
        Verify that check_exists extracts the Order from positional args.
        """
        # Arrange
        mock_velide.get_full_global_snapshot.return_value = create_snapshot([existing_delivery])
        strategy = DeliveryReconciliationStrategy(mock_velide, config)

        # Act - pass order as first positional arg
        result = await strategy.check_exists(order)

        # Assert
        assert result is not None
        assert result.id == "velide-123"

    @pytest.mark.asyncio
    async def test_extracts_order_from_kwargs(self, mock_velide, config, order, existing_delivery):
        """
        Verify that check_exists extracts the Order from keyword args.
        """
        # Arrange
        mock_velide.get_full_global_snapshot.return_value = create_snapshot([existing_delivery])
        strategy = DeliveryReconciliationStrategy(mock_velide, config)

        # Act - pass order as keyword arg
        result = await strategy.check_exists(some_other_arg="test", order=order)

        # Assert
        assert result is not None
        assert result.id == "velide-123"

    @pytest.mark.asyncio
    async def test_returns_none_when_no_order_in_args(self, mock_velide, config):
        """
        Verify that check_exists returns None when no Order is found in arguments.
        """
        # Arrange
        strategy = DeliveryReconciliationStrategy(mock_velide, config)

        # Act - pass no order
        result = await strategy.check_exists("not an order", some_kwarg="test")

        # Assert
        assert result is None
        mock_velide.get_full_global_snapshot.assert_not_called()

    @pytest.mark.asyncio
    async def test_prefers_args_over_kwargs(self, mock_velide, config, order, existing_delivery):
        """
        Verify that positional args are preferred over kwargs when both contain Order.
        """
        # Arrange
        mock_velide.get_full_global_snapshot.return_value = create_snapshot([existing_delivery])
        strategy = DeliveryReconciliationStrategy(mock_velide, config)

        different_order = create_test_order(customer_name="Jane Doe", address="456 Oak Ave")

        # Act - pass order in both args and kwargs
        # The first arg (order) matches existing_delivery ("John Doe")
        result = await strategy.check_exists(order, order=different_order)

        # Assert - should use the one from args
        assert result is not None
        assert result.metadata is not None
        assert result.metadata.customer_name == "John Doe"


class TestDeliveryReconciliationStrategyAddressMatching:
    """Test the internal _address_matches logic."""

    @pytest.fixture
    def mock_velide(self):
        return MagicMock()

    @pytest.fixture
    def config(self):
        return ReconciliationConfig(retry_reconciliation_enabled=True)

    def test_addresses_match_exact(self, mock_velide, config):
        strategy = DeliveryReconciliationStrategy(mock_velide, config)
        meta = MetadataResponse(address="123 Main St")
        assert strategy._address_matches(meta, "123 Main St") is True

    def test_addresses_match_substring(self, mock_velide, config):
        strategy = DeliveryReconciliationStrategy(mock_velide, config)
        meta = MetadataResponse(address="123 Main St, Apt 4")
        # "123 Main St" is inside "123 Main St, Apt 4"
        assert strategy._address_matches(meta, "123 Main St") is True

    def test_addresses_match_reverse_substring(self, mock_velide, config):
        strategy = DeliveryReconciliationStrategy(mock_velide, config)
        meta = MetadataResponse(address="123 Main St")
        # "123 Main St" is inside "123 Main St, Apt 4"
        assert strategy._address_matches(meta, "123 Main St, Apt 4") is True

    def test_addresses_match_case_insensitive(self, mock_velide, config):
        strategy = DeliveryReconciliationStrategy(mock_velide, config)
        meta = MetadataResponse(address="123 MAIN ST")
        assert strategy._address_matches(meta, "123 main st") is True

    def test_addresses_no_match(self, mock_velide, config):
        strategy = DeliveryReconciliationStrategy(mock_velide, config)
        meta = MetadataResponse(address="123 Main St")
        assert strategy._address_matches(meta, "456 Oak Ave") is False

    def test_addresses_match_rejects_short_strings(self, mock_velide, config):
        """Prevent matches on very short inputs like '10' matching '100 Street'"""
        strategy = DeliveryReconciliationStrategy(mock_velide, config)
        meta = MetadataResponse(address="100 Main St")
        
        # '10' is theoretically in '100 Main St', but should be rejected for length < 5
        assert strategy._address_matches(meta, "10") is False

    def test_addresses_match_empty_metadata(self, mock_velide, config):
        strategy = DeliveryReconciliationStrategy(mock_velide, config)
        meta = MetadataResponse(address=None)
        assert strategy._address_matches(meta, "123 Main St") is False


class TestDeliveryReconciliationStrategyProperties:
    """Test the properties of DeliveryReconciliationStrategy."""

    @pytest.fixture
    def mock_velide(self):
        """Create a mock Velide client."""
        return MagicMock()

    def test_delay_seconds_from_config(self, mock_velide):
        """
        Verify that delay_seconds property returns the configured value.
        """
        # Arrange
        config = ReconciliationConfig(
            retry_reconciliation_delay_seconds=5.5
        )
        strategy = DeliveryReconciliationStrategy(mock_velide, config)

        # Act & Assert
        assert strategy.delay_seconds == 5.5

class TestDeliveryReconciliationStrategyMatchingLogic:
    """
    Tests specific business rules in _find_best_match:
    - Time window filtering
    - Customer name filtering
    - Sorting/Selection logic
    """

    @pytest.fixture
    def mock_velide(self):
        mock = MagicMock()
        mock.get_full_global_snapshot = AsyncMock()
        return mock

    @pytest.fixture
    def config(self):
        return ReconciliationConfig(
            retry_reconciliation_enabled=True,
            retry_reconciliation_time_window_seconds=300.0  # 5 minutes
        )

    @pytest.mark.asyncio
    async def test_ignores_delivery_outside_time_window(self, mock_velide, config):
        """
        Verify that a delivery matching name and address is ignored 
        if it is older than the configured time window.
        """
        # Arrange
        now = datetime.now(timezone.utc)
        order = create_test_order()
        
        # Create an "old" delivery (10 minutes ago vs 5 min window)
        old_delivery = create_test_delivery(
            delivery_id="too-old",
            customer_name="John Doe",
            street="123 Main St"
        )
        # Override createdAt manually
        old_delivery.created_at = now - timedelta(minutes=10)

        mock_velide.get_full_global_snapshot.return_value = create_snapshot([old_delivery])
        strategy = DeliveryReconciliationStrategy(mock_velide, config)

        # Act
        result = await strategy.check_exists(order)

        # Assert
        assert result is None

    @pytest.mark.asyncio
    async def test_ignores_delivery_with_wrong_customer_name(self, mock_velide, config):
        """
        Verify that a delivery matching address and time is ignored
        if the customer name does not match.
        """
        # Arrange
        order = create_test_order(customer_name="John Doe")
        
        # Delivery has same address, recent time, but WRONG name
        wrong_name_delivery = create_test_delivery(
            delivery_id="wrong-person",
            customer_name="Jane Smith",
            street="123 Main St"
        )

        mock_velide.get_full_global_snapshot.return_value = create_snapshot([wrong_name_delivery])
        strategy = DeliveryReconciliationStrategy(mock_velide, config)

        # Act
        result = await strategy.check_exists(order)

        # Assert
        assert result is None

    @pytest.mark.asyncio
    async def test_selects_newest_delivery_when_multiple_matches_exist(self, mock_velide, config):
        """
        Verify that if multiple valid candidates exist, the strategy 
        returns the one created most recently.
        """
        # Arrange
        now = datetime.now(timezone.utc)
        order = create_test_order()

        # 1. Valid match, but older (3 minutes ago)
        older_match = create_test_delivery(
            delivery_id="older-id",
            customer_name="John Doe",
            street="123 Main St"
        )
        older_match.created_at = now - timedelta(minutes=3)

        # 2. Valid match, newer (1 minute ago)
        newer_match = create_test_delivery(
            delivery_id="newer-id",
            customer_name="John Doe",
            street="123 Main St"
        )
        newer_match.created_at = now - timedelta(minutes=1)

        # Return them in random order to ensure sorting works
        mock_velide.get_full_global_snapshot.return_value = create_snapshot([older_match, newer_match])
        strategy = DeliveryReconciliationStrategy(mock_velide, config)

        # Act
        result = await strategy.check_exists(order)

        # Assert
        assert result is not None
        assert result.id == "newer-id"