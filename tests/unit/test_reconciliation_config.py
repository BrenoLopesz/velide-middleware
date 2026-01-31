"""
Unit tests for the ReconciliationConfig validation.

These tests verify that the ReconciliationConfig model correctly validates
all the retry reconciliation fields with their constraints.
"""
import pytest
from pydantic import ValidationError

from config import ReconciliationConfig


class TestReconciliationConfigDefaults:
    """Test the default values for ReconciliationConfig."""

    def test_default_values(self):
        """
        Verify all new fields have correct default values.
        """
        # Arrange & Act
        config = ReconciliationConfig()

        # Assert - Existing fields
        assert config.enabled is True
        assert config.sync_interval_ms == 60_000
        assert config.cooldown_seconds == 45.0

        # Assert - New retry reconciliation fields
        assert config.retry_reconciliation_enabled is True
        assert config.retry_reconciliation_delay_seconds == 3.0
        assert config.retry_reconciliation_max_attempts == 2
        assert config.retry_reconciliation_time_window_seconds == 300.0

    def test_can_override_all_defaults(self):
        """
        Verify that all default values can be overridden.
        """
        # Arrange & Act
        config = ReconciliationConfig(
            retry_reconciliation_enabled=False,
            retry_reconciliation_delay_seconds=5.0,
            retry_reconciliation_max_attempts=4,
            retry_reconciliation_time_window_seconds=600.0
        )

        # Assert
        assert config.retry_reconciliation_enabled is False
        assert config.retry_reconciliation_delay_seconds == 5.0
        assert config.retry_reconciliation_max_attempts == 4
        assert config.retry_reconciliation_time_window_seconds == 600.0


class TestRetryReconciliationEnabledValidation:
    """Test the retry_reconciliation_enabled field validation."""

    def test_retry_reconciliation_enabled_accepts_true(self):
        """
        Verify that retry_reconciliation_enabled accepts True.
        """
        # Arrange & Act
        config = ReconciliationConfig(retry_reconciliation_enabled=True)

        # Assert
        assert config.retry_reconciliation_enabled is True

    def test_retry_reconciliation_enabled_accepts_false(self):
        """
        Verify that retry_reconciliation_enabled accepts False.
        """
        # Arrange & Act
        config = ReconciliationConfig(retry_reconciliation_enabled=False)

        # Assert
        assert config.retry_reconciliation_enabled is False

    def test_retry_reconciliation_enabled_coerces_string_truthy(self):
        """
        Verify that retry_reconciliation_enabled coerces string values (Pydantic V2 behavior).
        """
        # Act - Pydantic V2 coerces "yes" to True
        config = ReconciliationConfig(retry_reconciliation_enabled="yes")

        # Assert - coerced to True
        assert config.retry_reconciliation_enabled is True

    def test_retry_reconciliation_enabled_rejects_empty_string(self):
        """
        Verify that retry_reconciliation_enabled rejects empty string (Pydantic V2).
        """
        # Act & Assert - Pydantic V2 doesn't coerce empty string to bool
        with pytest.raises(ValidationError) as exc_info:
            ReconciliationConfig(retry_reconciliation_enabled="")

        assert "retry_reconciliation_enabled" in str(exc_info.value)


class TestRetryReconciliationDelaySecondsValidation:
    """Test the retry_reconciliation_delay_seconds field validation."""

    def test_retry_reconciliation_delay_seconds_must_be_non_negative(self):
        """
        Verify that retry_reconciliation_delay_seconds must be >= 0.
        """
        # Act & Assert - negative value should fail
        with pytest.raises(ValidationError) as exc_info:
            ReconciliationConfig(retry_reconciliation_delay_seconds=-1.0)

        assert "retry_reconciliation_delay_seconds" in str(exc_info.value)

    def test_retry_reconciliation_delay_seconds_accepts_zero(self):
        """
        Verify that retry_reconciliation_delay_seconds accepts 0.
        """
        # Arrange & Act
        config = ReconciliationConfig(retry_reconciliation_delay_seconds=0.0)

        # Assert
        assert config.retry_reconciliation_delay_seconds == 0.0

    def test_retry_reconciliation_delay_seconds_accepts_positive(self):
        """
        Verify that retry_reconciliation_delay_seconds accepts positive values.
        """
        # Arrange & Act
        config = ReconciliationConfig(retry_reconciliation_delay_seconds=10.5)

        # Assert
        assert config.retry_reconciliation_delay_seconds == 10.5

    def test_retry_reconciliation_delay_seconds_coerces_string(self):
        """
        Verify that retry_reconciliation_delay_seconds coerces string values (Pydantic V2 behavior).
        """
        # Act - Pydantic V2 coerces "5.5" to 5.5
        config = ReconciliationConfig(retry_reconciliation_delay_seconds="5.5")

        # Assert
        assert config.retry_reconciliation_delay_seconds == 5.5


class TestRetryReconciliationMaxAttemptsValidation:
    """Test the retry_reconciliation_max_attempts field validation."""

    def test_retry_reconciliation_max_attempts_range_ge_1(self):
        """
        Verify that retry_reconciliation_max_attempts must be >= 1.
        """
        # Act & Assert - zero should fail
        with pytest.raises(ValidationError) as exc_info:
            ReconciliationConfig(retry_reconciliation_max_attempts=0)

        assert "retry_reconciliation_max_attempts" in str(exc_info.value)

    def test_retry_reconciliation_max_attempts_range_le_5(self):
        """
        Verify that retry_reconciliation_max_attempts must be <= 5.
        """
        # Act & Assert - 6 should fail
        with pytest.raises(ValidationError) as exc_info:
            ReconciliationConfig(retry_reconciliation_max_attempts=6)

        assert "retry_reconciliation_max_attempts" in str(exc_info.value)

    def test_retry_reconciliation_max_attempts_accepts_minimum(self):
        """
        Verify that retry_reconciliation_max_attempts accepts 1 (minimum).
        """
        # Arrange & Act
        config = ReconciliationConfig(retry_reconciliation_max_attempts=1)

        # Assert
        assert config.retry_reconciliation_max_attempts == 1

    def test_retry_reconciliation_max_attempts_accepts_maximum(self):
        """
        Verify that retry_reconciliation_max_attempts accepts 5 (maximum).
        """
        # Arrange & Act
        config = ReconciliationConfig(retry_reconciliation_max_attempts=5)

        # Assert
        assert config.retry_reconciliation_max_attempts == 5

    def test_retry_reconciliation_max_attempts_accepts_middle_values(self):
        """
        Verify that retry_reconciliation_max_attempts accepts values between 1 and 5.
        """
        # Arrange & Act
        config = ReconciliationConfig(retry_reconciliation_max_attempts=3)

        # Assert
        assert config.retry_reconciliation_max_attempts == 3

    def test_retry_reconciliation_max_attempts_rejects_float_with_fraction(self):
        """
        Verify that retry_reconciliation_max_attempts rejects float with fractional part (Pydantic V2).
        """
        # Act & Assert - Pydantic V2 doesn't coerce 2.5 to int
        with pytest.raises(ValidationError) as exc_info:
            ReconciliationConfig(retry_reconciliation_max_attempts=2.5)

        assert "retry_reconciliation_max_attempts" in str(exc_info.value)


class TestRetryReconciliationTimeWindowValidation:
    """Test the retry_reconciliation_time_window_seconds field validation."""

    def test_retry_reconciliation_time_window_minimum_ge_60(self):
        """
        Verify that retry_reconciliation_time_window_seconds must be >= 60.
        """
        # Act & Assert - 59 should fail
        with pytest.raises(ValidationError) as exc_info:
            ReconciliationConfig(retry_reconciliation_time_window_seconds=59.0)

        assert "retry_reconciliation_time_window_seconds" in str(exc_info.value)

    def test_retry_reconciliation_time_window_accepts_minimum(self):
        """
        Verify that retry_reconciliation_time_window_seconds accepts 60 (minimum).
        """
        # Arrange & Act
        config = ReconciliationConfig(retry_reconciliation_time_window_seconds=60.0)

        # Assert
        assert config.retry_reconciliation_time_window_seconds == 60.0

    def test_retry_reconciliation_time_window_accepts_large_values(self):
        """
        Verify that retry_reconciliation_time_window_seconds accepts large values.
        """
        # Arrange & Act
        config = ReconciliationConfig(retry_reconciliation_time_window_seconds=3600.0)

        # Assert
        assert config.retry_reconciliation_time_window_seconds == 3600.0

    def test_retry_reconciliation_time_window_rejects_zero(self):
        """
        Verify that retry_reconciliation_time_window_seconds rejects 0.
        """
        # Act & Assert
        with pytest.raises(ValidationError) as exc_info:
            ReconciliationConfig(retry_reconciliation_time_window_seconds=0.0)

        assert "retry_reconciliation_time_window_seconds" in str(exc_info.value)

    def test_retry_reconciliation_time_window_rejects_negative(self):
        """
        Verify that retry_reconciliation_time_window_seconds rejects negative values.
        """
        # Act & Assert
        with pytest.raises(ValidationError) as exc_info:
            ReconciliationConfig(retry_reconciliation_time_window_seconds=-100.0)

        assert "retry_reconciliation_time_window_seconds" in str(exc_info.value)


class TestReconciliationConfigExistingFields:
    """Test that existing fields still work correctly."""

    def test_sync_interval_ms_validation(self):
        """
        Verify that sync_interval_ms still validates correctly (minimum 1000ms).
        """
        # Act & Assert - too small should fail
        with pytest.raises(ValidationError) as exc_info:
            ReconciliationConfig(sync_interval_ms=500)

        assert "sync_interval_ms" in str(exc_info.value) or "interval" in str(exc_info.value).lower()

    def test_cooldown_seconds_validation(self):
        """
        Verify that cooldown_seconds still validates correctly (non-negative).
        """
        # Act & Assert - negative should fail
        with pytest.raises(ValidationError) as exc_info:
            ReconciliationConfig(cooldown_seconds=-1.0)

        assert "cooldown" in str(exc_info.value).lower()

    def test_enabled_field_works(self):
        """
        Verify that the enabled field still works correctly.
        """
        # Arrange & Act
        config = ReconciliationConfig(enabled=False)

        # Assert
        assert config.enabled is False


class TestReconciliationConfigCombinedValidation:
    """Test multiple field validation scenarios."""

    def test_all_fields_together_valid(self):
        """
        Verify that all fields can be set together with valid values.
        """
        # Arrange & Act
        config = ReconciliationConfig(
            enabled=True,
            sync_interval_ms=120_000,
            cooldown_seconds=30.0,
            retry_reconciliation_enabled=True,
            retry_reconciliation_delay_seconds=5.0,
            retry_reconciliation_max_attempts=3,
            retry_reconciliation_time_window_seconds=600.0
        )

        # Assert
        assert config.enabled is True
        assert config.sync_interval_ms == 120_000
        assert config.cooldown_seconds == 30.0
        assert config.retry_reconciliation_enabled is True
        assert config.retry_reconciliation_delay_seconds == 5.0
        assert config.retry_reconciliation_max_attempts == 3
        assert config.retry_reconciliation_time_window_seconds == 600.0

    def test_partial_override_keeps_other_defaults(self):
        """
        Verify that partial override keeps other fields at defaults.
        """
        # Arrange & Act
        config = ReconciliationConfig(
            retry_reconciliation_max_attempts=1,
            retry_reconciliation_enabled=False
        )

        # Assert - overridden values
        assert config.retry_reconciliation_max_attempts == 1
        assert config.retry_reconciliation_enabled is False

        # Assert - other fields at defaults
        assert config.retry_reconciliation_delay_seconds == 3.0
        assert config.retry_reconciliation_time_window_seconds == 300.0
        assert config.sync_interval_ms == 60_000
        assert config.cooldown_seconds == 45.0
