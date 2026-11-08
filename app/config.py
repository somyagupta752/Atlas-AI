"""Compatibility import. The settings live in ``app.core.config``; existing imports keep working."""

from app.core.config import ConfigurationError, Settings, get_settings, load_settings

__all__ = ["ConfigurationError", "Settings", "get_settings", "load_settings"]
