from versions import is_newer


def test_double_digit_patch_version():
    assert is_newer("1.0.10", "1.0.9")


def test_double_digit_release_candidate():
    assert is_newer("3.0.0-rc10", "3.0.0-rc9")
