from versions import is_newer, needs_update


def test_patch_bump():
    assert is_newer("1.2.4", "1.2.3")


def test_same_version_is_not_newer():
    assert not is_newer("1.2.3", "1.2.3")


def test_release_candidates_compare_by_number():
    assert is_newer("2.0.0-rc2", "2.0.0-rc1")
    assert not is_newer("2.0.0-rc1", "2.0.0-rc2")


def test_double_digit_minor_version():
    assert is_newer("1.10.0", "1.9.0")


def test_update_available():
    assert needs_update("1.9.0", ["1.8.0", "1.10.0"])
