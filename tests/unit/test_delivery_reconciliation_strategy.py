"""
Unit tests for the DeliveryReconciliationStrategy.

These tests verify that the DeliveryReconciliationStrategy correctly implements
the reconciliation logic for delivery operations, checking if deliveries that
appeared to fail (due to timeout) actually succeeded on the server.
"""
import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from api.reconciliation.delivery_reconciliation_strategy import (
    DeliveryReconciliationStrategy
)
from models.velide_delivery_models import DeliveryResponse, Order, Location, LocationProperties, MetadataResponse
from config import ReconciliationConfig


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
    """Helper to create a test delivery response with required fields."""
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
            integrationName="TestSystem"
        )
    )


class TestDeliveryReconciliationStrategyCheckExists:
    """Test the check_exists method of DeliveryReconciliationStrategy."""

    @pytest.fixture
    def mock_velide(self):
        """Create a mock Velide client."""
        mock = MagicMock()
        mock.find_delivery_by_metadata = AsyncMock()
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
        mock_velide.find_delivery_by_metadata.return_value = existing_delivery
        strategy = DeliveryReconciliationStrategy(mock_velide, config)

        # Act
        result = await strategy.check_exists(order)

        # Assert
        assert result is not None
        assert result.id == "velide-123"
        mock_velide.find_delivery_by_metadata.assert_called_once_with(
            customer_name=order.customer_name,
            address=order.address,
            time_window_seconds=config.retry_reconciliation_time_window_seconds
        )

    @pytest.mark.asyncio
    async def test_check_exists_returns_none_when_no_match(self, mock_velide, config, order):
        """
        Verify that check_exists returns None when no matching delivery exists.
        """
        # Arrange
        mock_velide.find_delivery_by_metadata.return_value = None
        strategy = DeliveryReconciliationStrategy(mock_velide, config)

        # Act
        result = await strategy.check_exists(order)

        # Assert
        assert result is None
        mock_velide.find_delivery_by_metadata.assert_called_once()

    @pytest.mark.asyncio
    async def test_check_exists_handles_exception(self, mock_velide, config, order):
        """
        Verify that check_exists returns None on API error to allow retry to proceed.
        """
        # Arrange
        mock_velide.find_delivery_by_metadata.side_effect = Exception("API Error")
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
        mock_velide.find_delivery_by_metadata.return_value = existing_delivery
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
        mock_velide.find_delivery_by_metadata.return_value = existing_delivery
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
        mock_velide.find_delivery_by_metadata.assert_not_called()

    @pytest.mark.asyncio
    async def test_prefers_args_over_kwargs(self, mock_velide, config, order, existing_delivery):
        """
        Verify that positional args are preferred over kwargs when both contain Order.
        """
        # Arrange
        mock_velide.find_delivery_by_metadata.return_value = existing_delivery
        strategy = DeliveryReconciliationStrategy(mock_velide, config)

        different_order = create_test_order(customer_name="Jane Doe", address="456 Oak Ave")

        # Act - pass order in both args and kwargs
        result = await strategy.check_exists(order, order=different_order)

        # Assert - should use the one from args (John Doe)
        assert result is not None
        mock_velide.find_delivery_by_metadata.assert_called_with(
            customer_name="John Doe",  # From args, not kwargs
            address="123 Main St",
            time_window_seconds=300.0
        )


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

    def test_delay_seconds_returns_updated_config_value(self, mock_velide):
        """
        Verify that delay_seconds reflects changes to the config.
        """
        # Arrange
        config = ReconciliationConfig(
            retry_reconciliation_delay_seconds=3.0
        )
        strategy = DeliveryReconciliationStrategy(mock_velide, config)

        # Act - change config
        config.retry_reconciliation_delay_seconds = 7.0

        # Assert
        assert strategy.delay_seconds == 7.0