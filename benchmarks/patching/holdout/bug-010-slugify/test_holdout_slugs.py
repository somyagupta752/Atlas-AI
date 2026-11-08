from slugs import slugify


def test_other_accents():
    assert slugify("Crème brûlée") == "creme-brulee"


def test_tilde_and_umlaut():
    assert slugify("Ñandú über alles") == "nandu-uber-alles"
