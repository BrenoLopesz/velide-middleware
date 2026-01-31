"""
Unit tests for the Velide API reconciliation methods.

These tests verify that the Velide API client correctly implements
the _on_add_delivery_exception callback and initialization logic.

NOTE: Tests for fuzzy matching logic have been moved to 
test_delivery_reconciliation_strategy.py as that logic now resides 
in the Strategy class.
"""
import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import httpx

from api.velide import Velide
from config import ApiConfig, ReconciliationConfig, TargetSystem
from models.velide_delivery_models import (
    DeliveryResponse, Order, Location, LocationProperties, MetadataResponse
)


def create_test_order(customer_name="John Doe", address="123 Main St"):
    """Helper to create a test order."""
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


class TestVelideOnAddDeliveryException:
    """Test the _on_add_delivery_exception callback method."""

    @pytest.fixture
    def api_config(self):
        """Create test API config."""
        return ApiConfig(
            velide_server="https://test.velide.com/graphql",
            velide_websockets_server="wss://test.velide.com/ws",
            use_neighbourhood=False,
            use_ssl=True,
            timeout=30.0
        )

    @pytest.fixture
    def reconciliation_config(self):
        """Create test reconciliation config."""
        return ReconciliationConfig(
            retry_reconciliation_enabled=True,
            retry_reconciliation_delay_seconds=0.01,  # Fast for tests
            retry_reconciliation_time_window_seconds=300.0
        )

    @pytest.fixture
    def velide_with_reconciliation(self, api_config, reconciliation_config):
        """Create a Velide client with reconciliation enabled."""
        return Velide(
            access_token="test-token",
            api_config=api_config,
            target_system=TargetSystem.FARMAX,
            reconciliation_config=reconciliation_config
        )

    @pytest.fixture
    def velide_without_reconciliation(self, api_config):
        """Create a Velide client without reconciliation."""
        return Velide(
            access_token="test-token",
            api_config=api_config,
            target_system=TargetSystem.FARMAX,
            reconciliation_config=None
        )

    @pytest.mark.asyncio
    async def test_on_add_delivery_exception_only_triggers_on_timeout(
        self, velide_with_reconciliation
    ):
        """
        Verify that _on_add_delivery_exception only triggers reconciliation
        on TimeoutException, not on other exceptions.
        """
        # Arrange
        order = create_test_order()

        # Act - pass a non-timeout exception
        result = await velide_with_reconciliation._on_add_delivery_exception(
            exc=ValueError("Some other error"),
            attempt=1,
            args=(velide_with_reconciliation, order),
            kwargs={}
        )

        # Assert
        assert result is None  # No reconciliation attempted

    @pytest.mark.asyncio
    async def test_on_add_delivery_exception_returns_none_when_disabled(
        self, velide_without_reconciliation
    ):
        """
        Verify that _on_add_delivery_exception returns None when reconciliation
        is not configured.
        """
        # Arrange
        order = create_test_order()

        # Act
        result = await velide_without_reconciliation._on_add_delivery_exception(
            exc=httpx.TimeoutException("timeout"),
            attempt=1,
            args=(velide_without_reconciliation, order),
            kwargs={}
        )

        # Assert
        assert result is None

    @pytest.mark.asyncio
    async def test_on_add_delivery_exception_performs_reconciliation_on_timeout(
        self, velide_with_reconciliation, reconciliation_config
    ):
        """
        Verify that _on_add_delivery_exception performs reconciliation on timeout.
        """
        # Arrange
        order = create_test_order()
        existing_delivery = DeliveryResponse(
            id="velide-123",
            createdAt=datetime.now(timezone.utc),
            routeId=None,
            endedAt=None,
            location=Location(
                properties=LocationProperties(
                    street="123 Main St",
                    housenumber="",
                    neighbourhood=None,
                    name=None
                )
            ),
            metadata=MetadataResponse(
                customerName="John Doe",
                integrationName="TestSystem",
                address="123 Main St"  # Added address here too
            )
        )

        # Mock the reconciliation strategy
        velide_with_reconciliation._reconciliation_strategy.check_exists = AsyncMock(
            return_value=existing_delivery
        )

        # Act
        result = await velide_with_reconciliation._on_add_delivery_exception(
            exc=httpx.TimeoutException("timeout"),
            attempt=1,
            args=(velide_with_reconciliation, order),
            kwargs={}
        )

        # Assert
        assert result is not None
        assert result.id == "velide-123"
        velide_with_reconciliation._reconciliation_strategy.check_exists.assert_called_once()

    @pytest.mark.asyncio
    async def test_on_add_delivery_exception_returns_none_when_not_found(
        self, velide_with_reconciliation
    ):
        """
        Verify that _on_add_delivery_exception returns None when reconciliation
        doesn't find the delivery.
        """
        # Arrange
        order = create_test_order()

        # Mock the reconciliation strategy to return None
        velide_with_reconciliation._reconciliation_strategy.check_exists = AsyncMock(
            return_value=None
        )

        # Act
        result = await velide_with_reconciliation._on_add_delivery_exception(
            exc=httpx.TimeoutException("timeout"),
            attempt=1,
            args=(velide_with_reconciliation, order),
            kwargs={}
        )

        # Assert
        assert result is None


class TestVelideReconciliationInitialization:
    """Test the initialization of reconciliation in Velide."""

    @pytest.fixture
    def api_config(self):
        """Create test API config."""
        return ApiConfig(
            velide_server="https://test.velide.com/graphql",
            velide_websockets_server="wss://test.velide.com/ws",
            use_neighbourhood=False,
            use_ssl=True,
            timeout=30.0
        )

    def test_reconciliation_strategy_initialized_when_enabled(self, api_config):
        """
        Verify that reconciliation strategy is initialized when enabled in config.
        """
        # Arrange
        reconciliation_config = ReconciliationConfig(
            retry_reconciliation_enabled=True
        )

        # Act
        velide = Velide(
            access_token="test-token",
            api_config=api_config,
            target_system=TargetSystem.FARMAX,
            reconciliation_config=reconciliation_config
        )

        # Assert
        assert velide._reconciliation_strategy is not None
        assert velide._reconciliation_config is not None

    def test_reconciliation_strategy_not_initialized_when_disabled(self, api_config):
        """
        Verify that reconciliation strategy is NOT initialized when disabled.
        """
        # Arrange
        reconciliation_config = ReconciliationConfig(
            retry_reconciliation_enabled=False
        )

        # Act
        velide = Velide(
            access_token="test-token",
            api_config=api_config,
            target_system=TargetSystem.FARMAX,
            reconciliation_config=reconciliation_config
        )

        # Assert
        assert velide._reconciliation_strategy is None

    def test_reconciliation_strategy_not_initialized_without_config(self, api_config):
        """
        Verify that reconciliation strategy is NOT initialized when no config provided.
        """
        # Act
        velide = Velide(
            access_token="test-token",
            api_config=api_config,
            target_system=TargetSystem.FARMAX,
            reconciliation_config=None
        )

        # Assert
        assert velide._reconciliation_strategy is None
        assert velide._reconciliation_config is None