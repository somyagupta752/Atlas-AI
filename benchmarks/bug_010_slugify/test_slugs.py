from slugs import slugify


def test_basic():
    assert slugify("Hello, World!") == "hello-world"


def test_symbols_are_dropped():
    assert slugify("C++ & Rust!") == "c-rust"


def test_underscores_separate_words():
    assert slugify("snake_case_title") == "snake-case-title"


def test_slug_is_ascii():
    assert slugify("Ünïcödé").isascii()


def test_accented_letters_are_kept_as_ascii():
    assert slugify("Café au lait") == "cafe-au-lait"
