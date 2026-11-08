import pytest
from timeouts import request_timeout


def test_default_timeout():
    assert request_timeout({}) == 30


def test_hours():
    assert request_timeout({"timeout": "1h"}) == 3600


def test_invalid_unit():
    with pytest.raises(ValueError):
        request_timeout({"timeout": "10d"})
