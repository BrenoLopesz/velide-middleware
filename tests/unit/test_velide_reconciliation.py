"""
Unit tests for the Velide API reconciliation methods.

These tests verify that the Velide API client correctly implements
the find_delivery_by_metadata method and the _on_add_delivery_exception
callback for reconciliation-aware retry logic.
"""
import pytest
from datetime import datetime, timezone, timedelta
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


def create_mock_response_with_deliveries(deliveries_data):
    """Helper to create a mock HTTP response with delivery data."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json = MagicMock(return_value={
        "data": {
            "deliveries": deliveries_data,
            "deliverymen": []
        }
    })
    return mock_response


class TestVelideFindDeliveryByMetadata:
    """Test the find_delivery_by_metadata method."""

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
    def velide(self, api_config):
        """Create a Velide client with mocked client."""
        return Velide(
            access_token="test-token",
            api_config=api_config,
            target_system=TargetSystem.FARMAX
        )

    @pytest.mark.asyncio
    async def test_find_delivery_by_metadata_filters_by_customer_name(self, velide):
        """
        Verify that find_delivery_by_metadata filters by customer name (case-insensitive)
        AND requires metadata.address to match.
        """
        # Arrange
        now = datetime.now(timezone.utc)
        mock_response = create_mock_response_with_deliveries([
            {
                "id": "velide-123",
                "createdAt": now.isoformat(),
                "routeId": None,
                "endedAt": None,
                "metadata": {
                    "customerName": "John Doe",
                    "integrationName": "TestSystem",
                    "address": "123 Main St"  # MATCH
                }
            },
            {
                "id": "velide-456",
                "createdAt": now.isoformat(),
                "routeId": None,
                "endedAt": None,
                "metadata": {
                    "customerName": "Jane Smith",
                    "integrationName": "TestSystem",
                    "address": "123 Main St"
                }
            }
        ])

        velide._client = MagicMock()
        velide._client.post = AsyncMock(return_value=mock_response)

        # Act
        result = await velide.find_delivery_by_metadata(
            customer_name="john doe",  # lowercase to test case-insensitivity
            address="123 Main St",
            time_window_seconds=300.0
        )

        # Assert
        assert result is not None
        assert result.id == "velide-123"

    @pytest.mark.asyncio
    async def test_find_delivery_by_metadata_filters_by_time_window(self, velide):
        """
        Verify that find_delivery_by_metadata filters by time window.
        """
        # Arrange
        now = datetime.now(timezone.utc)
        mock_response = create_mock_response_with_deliveries([
            {
                "id": "velide-old",
                "createdAt": (now - timedelta(seconds=600)).isoformat(),  # 10 minutes ago
                "routeId": None,
                "endedAt": None,
                "metadata": {
                    "customerName": "John Doe",
                    "integrationName": "TestSystem",
                    "address": "123 Main St"
                }
            },
            {
                "id": "velide-recent",
                "createdAt": (now - timedelta(seconds=60)).isoformat(),  # 1 minute ago
                "routeId": None,
                "endedAt": None,
                "metadata": {
                    "customerName": "John Doe",
                    "integrationName": "TestSystem",
                    "address": "123 Main St"
                }
            }
        ])

        velide._client = MagicMock()
        velide._client.post = AsyncMock(return_value=mock_response)

        # Act - time window of 5 minutes
        result = await velide.find_delivery_by_metadata(
            customer_name="John Doe",
            address="123 Main St",
            time_window_seconds=300.0  # 5 minutes
        )

        # Assert - should only find the recent one
        assert result is not None
        assert result.id == "velide-recent"

    @pytest.mark.asyncio
    async def test_find_delivery_by_metadata_matches_address(self, velide):
        """
        Verify that find_delivery_by_metadata matches address using substring logic
        AGAINST METADATA (not location).
        """
        # Arrange
        now = datetime.now(timezone.utc)
        mock_response = create_mock_response_with_deliveries([
            {
                "id": "velide-123",
                "createdAt": now.isoformat(),
                "routeId": None,
                "endedAt": None,
                "metadata": {
                    "customerName": "John Doe",
                    "integrationName": "TestSystem",
                    # The stored metadata address (what we search against)
                    "address": "123 Main Street, Building A"
                }
            }
        ])

        velide._client = MagicMock()
        velide._client.post = AsyncMock(return_value=mock_response)

        # Act
        result = await velide.find_delivery_by_metadata(
            customer_name="John Doe",
            address="123 Main Street",  # Substring of metadata address
            time_window_seconds=300.0
        )

        # Assert
        assert result is not None
        assert result.id == "velide-123"

    @pytest.mark.asyncio
    async def test_find_delivery_by_metadata_returns_none_when_not_found(self, velide):
        """
        Verify that find_delivery_by_metadata returns None when no match is found.
        """
        # Arrange
        mock_response = create_mock_response_with_deliveries([])

        velide._client = MagicMock()
        velide._client.post = AsyncMock(return_value=mock_response)

        # Act
        result = await velide.find_delivery_by_metadata(
            customer_name="Non Existent",
            address="999 Nowhere St",
            time_window_seconds=300.0
        )

        # Assert
        assert result is None

    @pytest.mark.asyncio
    async def test_find_delivery_by_metadata_raises_exception(self, velide):
        """
        Verify that find_delivery_by_metadata RAISES exceptions (bubbling up).
        """
        # Arrange
        velide._client = MagicMock()
        velide._client.post = AsyncMock(side_effect=Exception("API Error"))

        # Act & Assert
        with pytest.raises(Exception, match="API Error"):
            await velide.find_delivery_by_metadata(
                customer_name="John Doe",
                address="123 Main St",
                time_window_seconds=300.0
            )


    @pytest.mark.asyncio
    async def test_find_delivery_by_metadata_handles_parsed_delivery_data(self, velide):
        """
        Verify that find_delivery_by_metadata correctly works with parsed delivery data.
        """
        # Arrange - Use a recent datetime that will pass the time window check
        now = datetime.now(timezone.utc)

        mock_response = create_mock_response_with_deliveries([
            {
                "id": "velide-123",
                "createdAt": now.isoformat(),
                "routeId": None,
                "endedAt": None,
                "metadata": {
                    "customerName": "John Doe",
                    "integrationName": "TestSystem",
                    "address": "123 Main St"
                }
            }
        ])

        velide._client = MagicMock()
        velide._client.post = AsyncMock(return_value=mock_response)

        # Act
        result = await velide.find_delivery_by_metadata(
            customer_name="John Doe",
            address="123 Main St",
            time_window_seconds=300.0
        )

        # Assert
        assert result is not None
        assert result.id == "velide-123"


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