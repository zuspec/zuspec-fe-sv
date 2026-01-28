"""Unit tests for configuration module."""
import pytest
from zuspec.fe.sv.config import SVToZuspecConfig


def test_config_defaults():
    """Test default configuration values."""
    config = SVToZuspecConfig()
    
    assert config.strict_mode is True
    assert config.ignore_covergroups is True
    assert config.ignore_assertions is False
    assert config.warn_only is False
    assert config.ignore_list == []


def test_config_custom():
    """Test custom configuration."""
    config = SVToZuspecConfig(
        strict_mode=False,
        ignore_covergroups=False,
        ignore_assertions=True,
        warn_only=True,
        ignore_list=["foo", "bar"],
    )
    
    assert config.strict_mode is False
    assert config.ignore_covergroups is False
    assert config.ignore_assertions is True
    assert config.warn_only is True
    assert config.ignore_list == ["foo", "bar"]
