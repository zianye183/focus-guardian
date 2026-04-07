"""
Tests for capture_window() — the per-window content extraction function.

These tests verify the function signature and basic contract. Full AX tree
testing requires macOS Accessibility permission and is covered by manual
testing with --once.
"""

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from screen_reader import capture_window


class TestCaptureWindowSignature:
    def test_returns_none_for_blocked_app(self):
        """Blocked apps should return a filtered record with no content."""
        with patch("screen_reader.is_app_blocked", return_value=True):
            result = capture_window(
                pid=1234,
                app_name="1Password",
                ax_window=None,
                window_id=100,
            )
        assert result is not None
        assert result["app"] == "1Password"
        assert result["title"] == "[blocked]"
        assert result["text"] == ""
        assert result["filtered"] == "app_blocked"
        assert result["window_id"] == 100

    def test_returns_none_for_hidden_app(self):
        """Hidden apps should return None."""
        with patch("screen_reader.is_app_blocked", return_value=False), \
             patch("screen_reader.is_app_hidden", return_value=True):
            result = capture_window(
                pid=1234,
                app_name="SomeApp",
                ax_window=None,
                window_id=100,
            )
        assert result is None

    def test_returns_dict_with_required_keys(self):
        """Even with no AX window, should return a valid record."""
        with patch("screen_reader.is_app_blocked", return_value=False), \
             patch("screen_reader.is_app_hidden", return_value=False):
            result = capture_window(
                pid=1234,
                app_name="Ghostty",
                ax_window=None,
                window_id=200,
            )
        assert result is not None
        assert "ts" in result
        assert result["app"] == "Ghostty"
        assert result["pid"] == 1234
        assert result["window_id"] == 200
        assert "title" in result
        assert "text" in result
