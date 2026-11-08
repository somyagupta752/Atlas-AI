from durations import to_seconds

DEFAULT_TIMEOUT = "30s"


def request_timeout(config: dict[str, str]) -> int:
    """Seconds an outgoing request may take, from config['timeout'] or the default."""
    return to_seconds(config.get("timeout", DEFAULT_TIMEOUT))
