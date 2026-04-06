"""
Tests for ax_observer module — AXObserver lifecycle management.

All pyobjc/CoreFoundation APIs are mocked since they require macOS
Accessibility permissions and a running GUI app to observe.
"""

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


@pytest.fixture
def mock_ax_apis():
    """Mock all pyobjc AXObserver and CoreFoundation APIs."""
    with patch("ax_observer.AXObserverCreate") as mock_create, \
         patch("ax_observer.AXObserverAddNotification") as mock_add, \
         patch("ax_observer.AXObserverRemoveNotification") as mock_remove, \
         patch("ax_observer.AXObserverGetRunLoopSource") as mock_source, \
         patch("ax_observer.AXUIElementCreateApplication") as mock_ax_app, \
         patch("ax_observer.CFRunLoopGetCurrent") as mock_loop, \
         patch("ax_observer.CFRunLoopAddSource") as mock_add_source, \
         patch("ax_observer.CFRunLoopRemoveSource") as mock_remove_source:

        mock_observer = MagicMock(name="AXObserverRef")
        mock_create.return_value = (0, mock_observer)
        mock_add.return_value = 0
        mock_source.return_value = MagicMock(name="CFRunLoopSourceRef")
        mock_ax_app.return_value = MagicMock(name="AXUIElementRef")
        mock_loop.return_value = MagicMock(name="CFRunLoopRef")

        yield {
            "create": mock_create,
            "add_notification": mock_add,
            "remove_notification": mock_remove,
            "get_source": mock_source,
            "ax_app": mock_ax_app,
            "get_loop": mock_loop,
            "add_source": mock_add_source,
            "remove_source": mock_remove_source,
            "observer": mock_observer,
            "source": mock_source.return_value,
            "loop": mock_loop.return_value,
        }


class TestStartObserving:
    def test_creates_observer_for_pid(self, mock_ax_apis):
        from ax_observer import start_observing, stop_observing
        stop_observing()  # ensure clean state

        callback = MagicMock()
        result = start_observing(1234, callback)

        assert result is True
        mock_ax_apis["create"].assert_called_once()
        # First arg is the PID
        assert mock_ax_apis["create"].call_args[0][0] == 1234

    def test_registers_focused_window_notification(self, mock_ax_apis):
        from ax_observer import start_observing, stop_observing
        stop_observing()

        callback = MagicMock()
        start_observing(1234, callback)

        mock_ax_apis["add_notification"].assert_called_once()
        call_args = mock_ax_apis["add_notification"].call_args[0]
        # Third arg is the notification name
        assert call_args[2] == "AXFocusedWindowChanged"

    def test_adds_source_to_run_loop(self, mock_ax_apis):
        from ax_observer import start_observing, stop_observing
        stop_observing()

        callback = MagicMock()
        start_observing(1234, callback)

        mock_ax_apis["get_source"].assert_called_once()
        mock_ax_apis["add_source"].assert_called_once()

    def test_returns_false_on_create_failure(self, mock_ax_apis):
        from ax_observer import start_observing, stop_observing
        stop_observing()

        mock_ax_apis["create"].return_value = (-25200, None)  # kAXErrorFailure

        callback = MagicMock()
        result = start_observing(1234, callback)

        assert result is False

    def test_returns_false_on_add_notification_failure(self, mock_ax_apis):
        from ax_observer import start_observing, stop_observing
        stop_observing()

        mock_ax_apis["add_notification"].return_value = -25207  # unsupported

        callback = MagicMock()
        result = start_observing(1234, callback)

        assert result is False

    def test_tears_down_previous_observer_before_new_one(self, mock_ax_apis):
        from ax_observer import start_observing, stop_observing
        stop_observing()

        callback = MagicMock()
        start_observing(1111, callback)
        start_observing(2222, callback)

        # Should have removed the run loop source from the first observer
        mock_ax_apis["remove_source"].assert_called_once()
        # Should have created two observers
        assert mock_ax_apis["create"].call_count == 2

    def test_skips_if_same_pid(self, mock_ax_apis):
        from ax_observer import start_observing, stop_observing
        stop_observing()

        callback = MagicMock()
        start_observing(1234, callback)
        start_observing(1234, callback)

        # Should only create once — second call is a no-op
        assert mock_ax_apis["create"].call_count == 1


class TestStopObserving:
    def test_removes_source_from_run_loop(self, mock_ax_apis):
        from ax_observer import start_observing, stop_observing
        stop_observing()
        mock_ax_apis["remove_source"].reset_mock()

        callback = MagicMock()
        start_observing(1234, callback)
        stop_observing()

        mock_ax_apis["remove_source"].assert_called_once_with(
            mock_ax_apis["loop"],
            mock_ax_apis["source"],
            "kCFRunLoopDefaultMode",
        )

    def test_noop_when_no_observer(self, mock_ax_apis):
        from ax_observer import stop_observing
        stop_observing()
        # Should not raise
        stop_observing()

    def test_clears_state_after_stop(self, mock_ax_apis):
        from ax_observer import start_observing, stop_observing
        stop_observing()

        callback = MagicMock()
        start_observing(1234, callback)
        stop_observing()

        # Starting a new observer should work (state was cleared)
        start_observing(5678, callback)
        assert mock_ax_apis["create"].call_count == 2
