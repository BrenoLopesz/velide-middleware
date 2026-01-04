import pytest
from datetime import date, time, datetime

# Import your classes and models here
# Adjust paths as necessary based on your project structure
from connectors.farmax.farmax_mapper import FarmaxMapper
from models.farmax_models import FarmaxDelivery, DeliveryLog
from models.velide_delivery_models import Order


class TestFarmaxMapperToOrder:
    """Tests for the to_order static method."""

    def test_to_order_happy_path(self):
        """
        Scenario: A perfectly valid FarmaxDelivery object.
        Expected: A populated Order object with timestamps 
                combined and IDs converted to string.
        """
        # Arrange
        raw_delivery = FarmaxDelivery(
            cd_venda=12345.0,
            nome="John Doe",
            fone="555-0199",
            hora_saida=time(10, 0),
            bairro="Downtown",
            tempendereco="123 Main St",
            tempreferencia="Near the park",
            data=date(2023, 10, 25),
            hora=time(14, 30),
        )

        # Act
        result = FarmaxMapper.to_order(raw_delivery)

        # Assert
        assert isinstance(result, Order)
        assert result.customer_name == "John Doe"
        assert result.address == "123 Main St"
        assert (
            result.neighbourhood == "Downtown"
        )  # Note: Model uses spelling 'neighbourhood'
        assert result.internal_id == "12345.0"

        # Verify datetime combination
        expected_dt = datetime(2023, 10, 25, 14, 30)
        assert result.created_at == expected_dt

    def test_to_order_sanitization_and_whitespace(self):
        """
        Scenario: Input strings have surrounding whitespace.
        Expected: _safe_str trims the whitespace in the resulting Order.
        """
        raw_delivery = FarmaxDelivery(
            cd_venda=1.0,
            nome="  Jane Doe  ",  # Needs trimming
            fone=None,
            tempendereco="  456 Lane  ",  # Needs trimming
            data=date(2023, 1, 1),
            hora=time(12, 0),
        )

        result = FarmaxMapper.to_order(raw_delivery)

        assert result.customer_name == "Jane Doe"
        assert result.address == "456 Lane"

    def test_to_order_handles_optional_fields_as_none(self):
        """
        Scenario: Optional fields in FarmaxDelivery are None.
        Expected: Order fields should be None, not "None" (string) or empty string.
        """
        raw_delivery = FarmaxDelivery(
            cd_venda=99.0,
            nome="Minimal User",
            tempendereco="No Reference Rd",
            data=date(2023, 1, 1),
            hora=time(12, 0),
            # Explicitly None
            fone=None,
            bairro=None,
            tempreferencia=None,
        )

        result = FarmaxMapper.to_order(raw_delivery)

        assert result.customer_contact is None
        assert result.reference is None
        assert result.neighbourhood is None

    def test_mapper_fails_on_empty_strings(self):
        """
        Scenario: Source allows empty string, but Destination (Order) does not.
        Expected: Mapper runs, but raises ValueError when creating the Order.
        """
        # 1. Create a valid Delivery, but with empty name
        raw_delivery = FarmaxDelivery(
            cd_venda=1.0,
            nome="",  # Empty string (often valid in SQL/Pydantic default)
            fone="555-0199",
            hora_saida=None,
            bairro=None,
            tempendereco="123 Main St",
            tempreferencia=None,
            data=date(2023, 10, 25),
            hora=time(14, 30),
        )

        # 2. Expect the Mapper to fail because Order rejects the empty string
        # Note: Your Order validator raises ValueError, not ValidationError
        with pytest.raises(ValueError) as excinfo:
            FarmaxMapper.to_order(raw_delivery)

        assert "validation error for Order" in str(excinfo.value)


class TestFarmaxMapperFilterIds:
    """Tests for the filter_new_insert_ids static method."""

    def test_filter_only_returns_untracked_inserts(self):
        """
        Scenario: Mixed logs (INSERT, UPDATE). Some INSERTs are tracked, others are not.
        Expected: Only returns INSERTs where is_tracked_check is False.
        """
        # Arrange
        logs = [
            # Case 1: INSERT, Not Tracked -> Should Keep
            DeliveryLog(id=1, cd_venda=101.0, action="INSERT", logdate=datetime.now()),
            # Case 2: INSERT, Already Tracked -> Should Discard
            DeliveryLog(id=2, cd_venda=102.0, action="INSERT", logdate=datetime.now()),
            # Case 3: UPDATE -> Should Discard regardless of tracking
            DeliveryLog(id=3, cd_venda=103.0, action="UPDATE", logdate=datetime.now()),
        ]

        # Mock the dependency: return True (tracked) only for ID 102.0
        def mock_is_tracked(sale_id: float) -> bool:
            return sale_id == 102.0

        # Act
        result = FarmaxMapper.filter_new_insert_ids(logs, mock_is_tracked)

        # Assert
        assert 101.0 in result
        assert 102.0 not in result
        assert 103.0 not in result
        assert len(result) == 1

    def test_filter_deduplicates_ids(self):
        """
        Scenario: Multiple logs exist for the same Sale ID 
                (e.g. duplicate inserts/logs).
        Expected: The returned set contains the ID only once.
        """
        logs = [
            DeliveryLog(id=1, cd_venda=55.0, action="INSERT", logdate=datetime.now()),
            DeliveryLog(id=2, cd_venda=55.0, action="INSERT", logdate=datetime.now()),
        ]

        result = FarmaxMapper.filter_new_insert_ids(logs, lambda x: False)

        assert len(result) == 1
        assert 55.0 in result

    def test_filter_case_insensitivity_defensive(self):
        """
        Scenario: Action comes in as 'insert' (lowercase) or malformed object.
        Expected: Mapper handles normalization (.upper()) and continues.
        """

        # Using a generic object to simulate a malformed/dynamic object
        # that might pass strict Pydantic checks if data source is messy
        class MockLog:
            def __init__(self, action, sale_id):
                self.action = action
                self.sale_id = sale_id

        logs = [
            MockLog(action="insert", sale_id=200.0),  # Lowercase
            MockLog(action="INSERT", sale_id=201.0),  # Uppercase
        ]

        result = FarmaxMapper.filter_new_insert_ids(logs, lambda x: False)

        assert 200.0 in result
        assert 201.0 in result

    def test_filter_skips_logs_without_sale_id(self):
        """
        Scenario: A log entry somehow has None for sale_id.
        Expected: It is skipped safely.
        """

        # We have to bypass Pydantic validation to force a None sale_id
        # or simulate an object that looks like the model but isn't
        class BadLog:
            action = "INSERT"
            sale_id = None

        logs = [BadLog()]

        result = FarmaxMapper.filter_new_insert_ids(logs, lambda x: False)
        assert len(result) == 0
