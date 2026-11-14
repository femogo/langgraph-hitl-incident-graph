"""Smoke tests for package installation and typed configuration."""

from pytest import MonkeyPatch

from incident_responder import __version__
from incident_responder.config import get_settings


def test_package_version() -> None:
    """Verify that the package imports cleanly and exposes its version."""
    assert __version__ == "0.1.0"


def test_settings_override(monkeypatch: MonkeyPatch) -> None:
    """Verify that environment variables override default settings."""
    monkeypatch.setenv("OLLAMA_MODEL", "llama3.2")
    monkeypatch.setenv("APP_ENV", "testing")
    get_settings.cache_clear()

    settings = get_settings()
    assert settings.ollama_model == "llama3.2"
    assert settings.app_env == "testing"
