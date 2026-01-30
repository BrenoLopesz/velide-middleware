"""
Unit tests for the async_retry decorator with on_exception callback support.

These tests verify that the enhanced @async_retry decorator properly handles
the on_exception callback for reconciliation-aware retry logic.
"""
import pytest
import httpx
from unittest.mock import AsyncMock, MagicMock, patch

from utils.async_retry import async_retry


class TestAsyncRetryOnExceptionCallback:
    """Test the on_exception callback functionality in async_retry decorator."""

    @pytest.mark.asyncio
    async def test_on_exception_callback_prevents_retry(self):
        """
        Verify that when the on_exception callback returns a non-None value,
        that value is returned immediately and retries are skipped.
        """
        # Arrange
        callback_result = {"id": "existing-delivery", "status": "found"}
        mock_callback = MagicMock(return_value=callback_result)
        mock_operation = AsyncMock(side_effect=httpx.TimeoutException("timeout"))

        @async_retry(
            operation_desc="test operation",
            max_retries=3,
            initial_delay=0.01,
            on_exception=mock_callback
        )
        async def operation():
            return await mock_operation()

        # Act
        result = await operation()

        # Assert
        assert result == callback_result
        assert mock_operation.call_count == 1  # No retry attempted
        mock_callback.assert_called_once()
        # Verify callback received correct arguments
        call_args = mock_callback.call_args[0]
        assert isinstance(call_args[0], httpx.TimeoutException)
        assert call_args[1] == 1  # First attempt

    @pytest.mark.asyncio
    async def test_on_exception_callback_allows_retry_when_returns_none(self):
        """
        Verify that when the on_exception callback returns None,
        normal retry logic continues.
        """
        # Arrange
        mock_callback = MagicMock(return_value=None)
        mock_operation = AsyncMock(side_effect=[
            httpx.TimeoutException("timeout"),
            "success"
        ])

        @async_retry(
            operation_desc="test operation",
            max_retries=3,
            initial_delay=0.01,
            on_exception=mock_callback
        )
        async def operation():
            return await mock_operation()

        # Act
        result = await operation()

        # Assert
        assert result == "success"
        assert mock_operation.call_count == 2  # Original + 1 retry
        assert mock_callback.call_count == 1  # Called on first exception

    @pytest.mark.asyncio
    async def test_on_exception_not_called_on_success(self):
        """
        Verify that the on_exception callback is NOT called when
        the operation succeeds on the first attempt.
        """
        # Arrange
        mock_callback = MagicMock(return_value=None)
        mock_operation = AsyncMock(return_value="success")

        @async_retry(
            operation_desc="test operation",
            max_retries=3,
            on_exception=mock_callback
        )
        async def operation():
            return await mock_operation()

        # Act
        result = await operation()

        # Assert
        assert result == "success"
        assert mock_operation.call_count == 1
        mock_callback.assert_not_called()  # Never called on success

    @pytest.mark.asyncio
    async def test_async_on_exception_callback_supported(self):
        """
        Verify that async callbacks work properly with the decorator.
        """
        # Arrange
        callback_result = {"id": "reconciled", "source": "async_callback"}
        mock_async_callback = AsyncMock(return_value=callback_result)
        mock_operation = AsyncMock(side_effect=httpx.TimeoutException("timeout"))

        @async_retry(
            operation_desc="test operation",
            max_retries=3,
            initial_delay=0.01,
            on_exception=mock_async_callback
        )
        async def operation():
            return await mock_operation()

        # Act
        result = await operation()

        # Assert
        assert result == callback_result
        assert mock_operation.call_count == 1  # No retry
        mock_async_callback.assert_called_once()
        # Verify it was awaited properly
        assert mock_async_callback.await_count == 1

    @pytest.mark.asyncio
    async def test_backward_compatibility_without_callback(self):
        """
        Verify that the decorator works correctly when no on_exception
        callback is provided (backward compatibility).
        """
        # Arrange
        mock_operation = AsyncMock(side_effect=[
            httpx.TimeoutException("timeout"),
            httpx.TimeoutException("timeout"),
            "success"
        ])

        @async_retry(
            operation_desc="test operation",
            max_retries=3,
            initial_delay=0.01
        )
        async def operation():
            return await mock_operation()

        # Act
        result = await operation()

        # Assert
        assert result == "success"
        assert mock_operation.call_count == 3

    @pytest.mark.asyncio
    async def test_callback_receives_args_and_kwargs(self):
        """
        Verify that the callback receives the original args and kwargs
        passed to the decorated function.
        """
        # Arrange
        received_args = None
        received_kwargs = None

        def capture_callback(exc, attempt, args, kwargs):
            nonlocal received_args, received_kwargs
            received_args = args
            received_kwargs = kwargs
            return None

        mock_operation = AsyncMock(side_effect=[
            httpx.TimeoutException("timeout"),
            "success"
        ])

        @async_retry(
            operation_desc="test operation",
            max_retries=2,
            initial_delay=0.01,
            on_exception=capture_callback
        )
        async def operation(arg1, arg2, key1=None, key2=None):
            return await mock_operation()

        # Act
        await operation("first", "second", key1="value1", key2="value2")

        # Assert
        assert received_args == ("first", "second")
        assert received_kwargs == {"key1": "value1", "key2": "value2"}

    @pytest.mark.asyncio
    async def test_callback_called_on_each_exception(self):
        """
        Verify that the callback is called for each exception until
        either it returns a result or max retries are reached.
        """
        # Arrange
        mock_callback = MagicMock(return_value=None)
        mock_operation = AsyncMock(side_effect=[
            httpx.TimeoutException("timeout1"),
            httpx.TimeoutException("timeout2"),
            "success"
        ])

        @async_retry(
            operation_desc="test operation",
            max_retries=3,
            initial_delay=0.01,
            on_exception=mock_callback
        )
        async def operation():
            return await mock_operation()

        # Act
        await operation()

        # Assert
        assert mock_callback.call_count == 2  # Called on each exception
        # Verify attempt numbers are correct
        call_args_list = mock_callback.call_args_list
        assert call_args_list[0][0][1] == 1  # First attempt
        assert call_args_list[1][0][1] == 2  # Second attempt

    @pytest.mark.asyncio
    async def test_callback_result_returned_immediately(self):
        """
        Verify that when callback returns a result, it's returned
        immediately without waiting for sleep or further retries.
        """
        # Arrange
        import asyncio

        callback_result = "reconciled_result"
        mock_callback = MagicMock(return_value=callback_result)

        sleep_times = []
        original_sleep = asyncio.sleep

        async def tracked_sleep(delay):
            sleep_times.append(delay)
            await original_sleep(0)

        mock_operation = AsyncMock(side_effect=httpx.TimeoutException("timeout"))

        @async_retry(
            operation_desc="test operation",
            max_retries=3,
            initial_delay=1.0,  # Would normally sleep 1 second
            on_exception=mock_callback
        )
        async def operation():
            return await mock_operation()

        # Act
        with patch('asyncio.sleep', tracked_sleep):
            result = await operation()

        # Assert
        assert result == callback_result
        assert len(sleep_times) == 0  # No sleep occurred

    @pytest.mark.asyncio
    async def test_exception_propagated_when_no_callback_and_max_retries(self):
        """
        Verify that when no callback is provided and max retries are
        reached, the original exception is raised.
        """
        # Arrange
        mock_operation = AsyncMock(
            side_effect=httpx.TimeoutException("persistent timeout")
        )

        @async_retry(
            operation_desc="test operation",
            max_retries=2,
            initial_delay=0.01
        )
        async def operation():
            return await mock_operation()

        # Act & Assert
        with pytest.raises(httpx.TimeoutException) as exc_info:
            await operation()

        assert "persistent timeout" in str(exc_info.value)
        assert mock_operation.call_count == 2

    @pytest.mark.asyncio
    async def test_callback_can_return_falsy_but_not_none(self):
        """
        Verify that callback returning falsy values (0, False, empty string)
        does NOT skip retry - only None allows retry.
        """
        # Arrange
        mock_callback = MagicMock(return_value="")  # Empty string is falsy but not None
        mock_operation = AsyncMock(side_effect=httpx.TimeoutException("timeout"))

        @async_retry(
            operation_desc="test operation",
            max_retries=3,
            initial_delay=0.01,
            on_exception=mock_callback
        )
        async def operation():
            return await mock_operation()

        # Act
        result = await operation()

        # Assert
        assert result == ""  # Empty string returned, retry skipped
        assert mock_operation.call_count == 1


class TestAsyncRetryBasicFunctionality:
    """Test basic async_retry functionality to ensure no regression."""

    @pytest.mark.asyncio
    async def test_success_on_first_attempt(self):
        """Should return result immediately on success."""
        mock_func = AsyncMock(return_value="success")

        @async_retry(operation_desc="test op")
        async def operation():
            return await mock_func()

        result = await operation()

        assert result == "success"
        assert mock_func.call_count == 1

    @pytest.mark.asyncio
    async def test_retry_on_configured_exceptions(self):
        """Should retry on configured exceptions."""
        mock_func = AsyncMock(side_effect=[
            httpx.TimeoutException("timeout"),
            "success"
        ])

        @async_retry(operation_desc="test op", max_retries=3, initial_delay=0.01)
        async def operation():
            return await mock_func()

        result = await operation()

        assert result == "success"
        assert mock_func.call_count == 2

    @pytest.mark.asyncio
    async def test_no_retry_on_unconfigured_exceptions(self):
        """Should not retry on exceptions not in the exceptions tuple."""
        mock_func = AsyncMock(side_effect=ValueError("not a retry exception"))

        @async_retry(operation_desc="test op", max_retries=3)
        async def operation():
            return await mock_func()

        with pytest.raises(ValueError):
            await operation()

        assert mock_func.call_count == 1  # No retry

    @pytest.mark.asyncio
    async def test_exponential_backoff(self):
        """Should use exponential backoff between retries."""
        import asyncio

        sleep_delays = []
        original_sleep = asyncio.sleep

        async def tracked_sleep(delay):
            sleep_delays.append(delay)
            await original_sleep(0)

        mock_func = AsyncMock(side_effect=[
            httpx.TimeoutException("timeout1"),
            httpx.TimeoutException("timeout2"),
            "success"
        ])

        @async_retry(
            operation_desc="test op",
            max_retries=3,
            initial_delay=1.0,
            backoff_factor=2.0
        )
        async def operation():
            return await mock_func()

        with patch('asyncio.sleep', tracked_sleep):
            await operation()

        # Assert exponential backoff: 1.0, 2.0
        assert sleep_delays == [1.0, 2.0]
