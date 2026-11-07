import pytest
from config_loader import DEFAULT_PORT, ConfigError, load_port


def test_missing_port_uses_default():
    assert load_port({}) == DEFAULT_PORT


def test_valid_port():
    assert load_port({"PORT": "9000"}) == 9000


def test_non_numeric_port_is_an_error():
    with pytest.raises(ConfigError):
        load_port({"PORT": "abc"})


def test_out_of_range_port_is_an_error():
    with pytest.raises(ConfigError):
        load_port({"PORT": "70000"})
