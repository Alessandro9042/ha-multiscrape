"""Tests for the coordinator module."""
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant

from custom_components.multiscrape.const import MAX_RETRIES
from custom_components.multiscrape.coordinator import (
    ContentRequestManager, MultiscrapeDataUpdateCoordinator)
from custom_components.multiscrape.scrape_context import ScrapeContext


@pytest.mark.unit
@pytest.mark.async_test
@pytest.mark.timeout(5)
async def test_content_request_manager_get_content_basic(
    content_request_manager, mock_http_session
):
    """Test basic content retrieval without form submission."""
    # Arrange
    mock_http_session.async_request.return_value.text = "<html>Test Content</html>"

    # Act
    result = await content_request_manager.get_content()

    # Assert
    assert result == "<html>Test Content</html>"
    mock_http_session.async_request.assert_called_once()


@pytest.mark.unit
@pytest.mark.async_test
@pytest.mark.timeout(5)
async def test_content_request_manager_with_form_submission(
    mock_resource_renderer, mock_http_response
):
    """Test content retrieval when form submission returns content."""
    # Arrange
    mock_session = AsyncMock()
    mock_session.ensure_authenticated = AsyncMock(
        return_value="<html>Form Response</html>"
    )
    mock_session.form_variables = {"var": "value"}
    mock_session.invalidate_auth = MagicMock()
    mock_session.async_request = AsyncMock()

    manager = ContentRequestManager(
        config_name="test",
        session=mock_session,
        resource_renderer=mock_resource_renderer,
    )

    # Act
    result = await manager.get_content()

    # Assert
    assert result == "<html>Form Response</html>"
    mock_session.ensure_authenticated.assert_called_once()
    # HTTP request should NOT be called since form submission returned content
    mock_session.async_request.assert_not_called()


@pytest.mark.unit
@pytest.mark.async_test
@pytest.mark.timeout(5)
async def test_content_request_manager_form_submission_no_result(
    mock_resource_renderer, mock_http_response
):
    """Test content retrieval when form submission returns None (form has own resource)."""
    # Arrange
    mock_session = AsyncMock()
    mock_session.ensure_authenticated = AsyncMock(return_value=None)
    mock_session.form_variables = {"var": "value"}
    mock_session.invalidate_auth = MagicMock()
    mock_session.async_request = AsyncMock(
        return_value=mock_http_response(text="<html>Page Content</html>")
    )

    manager = ContentRequestManager(
        config_name="test",
        session=mock_session,
        resource_renderer=mock_resource_renderer,
    )

    # Act
    result = await manager.get_content()

    # Assert
    assert result == "<html>Page Content</html>"
    mock_session.async_request.assert_called_once()


@pytest.mark.integration
@pytest.mark.async_test
@pytest.mark.timeout(10)
async def test_coordinator_successful_update(coordinator, mock_http_session, scraper):
    """Test successful data update through coordinator."""
    # Arrange
    mock_http_session.async_request.return_value.text = "<html>Updated Content</html>"

    # Act
    await coordinator.async_refresh()

    # Assert
    assert coordinator.last_update_success
    assert not coordinator.update_error
    # Verify the scraper received the content
    assert scraper._data == "<html>Updated Content</html>"


@pytest.mark.integration
@pytest.mark.async_test
@pytest.mark.timeout(10)
async def test_coordinator_update_failure(coordinator, mock_http_session):
    """Test coordinator behavior on update failure."""
    # Arrange
    mock_http_session.async_request.side_effect = Exception("Network error")

    # Act
    await coordinator.async_refresh()

    # Assert
    assert coordinator.update_error
    # The coordinator should handle the exception gracefully


@pytest.mark.unit
@pytest.mark.async_test
@pytest.mark.timeout(5)
async def test_coordinator_request_reauth(
    coordinator, mock_http_session
):
    """Test that request_reauth sets the force_reauth flag."""
    # Act
    coordinator.request_reauth()

    # Assert
    assert coordinator._force_reauth is True


@pytest.mark.integration
@pytest.mark.async_test
@pytest.mark.timeout(10)
async def test_coordinator_force_reauth_lifecycle(
    coordinator, mock_http_session
):
    """Verify full flag lifecycle: request_reauth -> pass force_reauth -> invalidate_auth -> reset."""
    # Simulate entity reporting a scrape exception
    coordinator.request_reauth()
    assert coordinator._force_reauth is True

    # Next update should pass force_reauth=True, then reset the flag
    await coordinator._async_update_data()

    # invalidate_auth should have been called (via ContentRequestManager.get_content)
    mock_http_session.invalidate_auth.assert_called_once()
    # Flag should be reset after successful update
    assert coordinator._force_reauth is False


@pytest.mark.integration
@pytest.mark.async_test
@pytest.mark.timeout(10)
async def test_coordinator_with_zero_scan_interval(
    hass: HomeAssistant,
    content_request_manager,
    mock_file_manager,
    scraper,
    mock_http_session,
):
    """Test coordinator with scan_interval set to zero (manual updates only).

    When scan_interval is 0, the coordinator should:
    1. Set _update_interval to None (disables automatic updates)
    2. Only update when manually triggered via async_request_refresh()
    """
    # Arrange
    coordinator = MultiscrapeDataUpdateCoordinator(
        config_name="test_coordinator",
        hass=hass,
        request_manager=content_request_manager,
        file_manager=mock_file_manager,
        scraper=scraper,
        update_interval=timedelta(seconds=0),
    )

    # Assert - interval is disabled
    assert coordinator._update_interval is None

    # Verify manual update still works
    mock_http_session.async_request.return_value.text = "<html>Manual Update</html>"
    await coordinator.async_request_refresh()
    await hass.async_block_till_done()

    assert scraper._data == "<html>Manual Update</html>"
    assert coordinator.last_update_success

    await coordinator.async_shutdown()


# ============================================================================
# Retry logic tests (scan_interval=0)
# ============================================================================


@pytest.mark.integration
@pytest.mark.async_test
@pytest.mark.timeout(10)
async def test_coordinator_zero_interval_retries_on_failure(
    hass: HomeAssistant,
    content_request_manager,
    mock_file_manager,
    scraper,
    mock_http_session,
):
    """Test that zero-interval coordinator schedules a retry on failure.

    When scan_interval=0, there's no automatic refresh interval. On failure,
    the coordinator should schedule a one-shot retry instead of silently failing.
    """
    coordinator = MultiscrapeDataUpdateCoordinator(
        config_name="test_retry",
        hass=hass,
        request_manager=content_request_manager,
        file_manager=mock_file_manager,
        scraper=scraper,
        update_interval=timedelta(seconds=0),
    )
    assert coordinator._update_interval is None
    assert coordinator._retry_count == 0

    # Simulate failed content retrieval
    mock_http_session.async_request.side_effect = Exception("Network error")

    with patch(
        "custom_components.multiscrape.coordinator.event.async_track_point_in_utc_time"
    ) as mock_track:
        await coordinator._async_update_data()

    # Assert
    assert coordinator._retry_count == 1
    assert coordinator.update_error is True
    mock_track.assert_called_once()

    # The scheduled callback should be a coroutine function that, when called,
    # triggers a refresh via the coordinator's own public API (not the
    # debounced async_request_refresh, which can silently drop retries).
    call_args = mock_track.call_args[0]
    scheduled_hass, callback, _when = call_args
    assert scheduled_hass is coordinator.hass
    assert asyncio.iscoroutinefunction(callback)

    with patch.object(
        coordinator, "async_refresh", new=AsyncMock()
    ) as mock_refresh:
        await callback(None)
        mock_refresh.assert_called_once()

    await coordinator.async_shutdown()

@pytest.mark.integration
@pytest.mark.async_test
@pytest.mark.timeout(10)
async def test_coordinator_zero_interval_stops_after_max_retries(
    hass: HomeAssistant,
    content_request_manager,
    mock_file_manager,
    scraper,
    mock_http_session,
    caplog,
):
    """Test that zero-interval coordinator stops retrying after MAX_RETRIES."""
    coordinator = MultiscrapeDataUpdateCoordinator(
        config_name="test_max_retry",
        hass=hass,
        request_manager=content_request_manager,
        file_manager=mock_file_manager,
        scraper=scraper,
        update_interval=timedelta(seconds=0),
    )
    coordinator._retry_count = MAX_RETRIES

    mock_http_session.async_request.side_effect = Exception("Network error")

    with patch(
        "custom_components.multiscrape.coordinator.event.async_track_point_in_utc_time"
    ) as mock_track:
        await coordinator._async_update_data()

    # Assert - no more retries scheduled, and the counter is reset so a
    # manual trigger (or the next external retry) starts from zero again.
    assert coordinator._retry_count == 0
    mock_track.assert_not_called()
    assert "please manually retry with trigger service" in caplog.text

    await coordinator.async_shutdown()

@pytest.mark.integration
@pytest.mark.async_test
@pytest.mark.timeout(10)
async def test_coordinator_zero_interval_resets_retry_on_success(
    hass: HomeAssistant,
    content_request_manager,
    mock_file_manager,
    scraper,
    mock_http_session,
):
    """Test that a successful update resets the retry counter."""
    coordinator = MultiscrapeDataUpdateCoordinator(
        config_name="test_reset_retry",
        hass=hass,
        request_manager=content_request_manager,
        file_manager=mock_file_manager,
        scraper=scraper,
        update_interval=timedelta(seconds=0),
    )
    coordinator._retry_count = 2

    mock_http_session.async_request.return_value.text = "<html>Success</html>"

    await coordinator._async_update_data()

    # Assert - retry count reset
    assert coordinator._retry_count == 0
    assert coordinator.update_error is False


@pytest.mark.integration
@pytest.mark.async_test
@pytest.mark.timeout(10)
async def test_coordinator_nonzero_interval_does_not_retry(
    coordinator, mock_http_session
):
    """Test that non-zero interval coordinator does not use retry mechanism.

    Retry scheduling is only for scan_interval=0 configs. Normal interval-based
    coordinators rely on the next scheduled refresh instead.
    """
    assert coordinator._retry_count == 0

    mock_http_session.async_request.side_effect = Exception("Network error")

    await coordinator._async_update_data()

    # Assert - retry count NOT incremented (retry logic is only for interval=None)
    assert coordinator._retry_count == 0
    assert coordinator.update_error is True

@pytest.mark.integration
@pytest.mark.async_test
@pytest.mark.timeout(10)
async def test_coordinator_custom_max_retries(
    hass: HomeAssistant,
    content_request_manager,
    mock_file_manager,
    scraper,
    mock_http_session,
    caplog,
):
    """Test that a non-default max_retries value is honored.

    With max_retries=0, the very first failure should skip scheduling a
    retry entirely and go straight to the "please manually retry" branch —
    useful when an external mechanism already handles retries/coordination
    for this resource.
    """
    coordinator = MultiscrapeDataUpdateCoordinator(
        config_name="test_custom_retry",
        hass=hass,
        request_manager=content_request_manager,
        file_manager=mock_file_manager,
        scraper=scraper,
        update_interval=timedelta(seconds=0),
        max_retries=0,
    )
    assert coordinator._max_retries == 0

    mock_http_session.async_request.side_effect = Exception("Network error")

    with patch(
        "custom_components.multiscrape.coordinator.event.async_track_point_in_utc_time"
    ) as mock_track:
        await coordinator._async_update_data()

    assert coordinator._retry_count == 0
    mock_track.assert_not_called()
    assert "please manually retry with trigger service" in caplog.text

    await coordinator.async_shutdown()
    
# ============================================================================
# _prepare_new_run tests
# ============================================================================


@pytest.mark.integration
@pytest.mark.async_test
@pytest.mark.timeout(10)
async def test_coordinator_prepare_new_run_clears_state(
    coordinator, mock_file_manager
):
    """Test that _prepare_new_run resets state for a fresh update cycle."""
    # Set up dirty state
    coordinator.update_error = True

    await coordinator._prepare_new_run()

    # Assert
    assert coordinator.update_error is False
    mock_file_manager.empty_folder.assert_called_once()


# ============================================================================
# Form variables property chain
# ============================================================================


@pytest.mark.unit
@pytest.mark.async_test
@pytest.mark.timeout(5)
async def test_coordinator_scrape_context_wraps_form_variables(
    coordinator, mock_http_session
):
    """Test that coordinator.scrape_context wraps session.form_variables in a ScrapeContext."""
    mock_http_session.form_variables = {"x-token": "abc123"}
    ctx = coordinator.scrape_context
    assert isinstance(ctx, ScrapeContext)
    assert ctx.form_variables == {"x-token": "abc123"}
    assert ctx.current_value is None
