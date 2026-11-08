"""Exploratory: no test or docstring says how a release compares with its own candidates."""

from versions import is_newer


def test_release_is_newer_than_its_candidate():
    assert is_newer("2.0.0", "2.0.0-rc1")
