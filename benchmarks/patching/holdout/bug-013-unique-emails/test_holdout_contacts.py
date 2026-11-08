from contacts import unique_emails


def test_first_spelling_wins_for_every_address():
    assert unique_emails(["B@x.com", "a@x.com", "b@X.COM", "A@X.com"]) == ["B@x.com", "a@x.com"]
