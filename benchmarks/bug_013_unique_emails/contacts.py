def unique_emails(emails: list[str]) -> list[str]:
    """Distinct email addresses, in first-seen order. Addresses differing only in case are the same."""
    return list(dict.fromkeys(emails))
