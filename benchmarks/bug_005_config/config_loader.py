DEFAULT_PORT = 8080


class ConfigError(Exception):
    """The configuration is present but invalid."""


def load_port(env: dict[str, str]) -> int:
    """Read PORT from the environment. Missing means the default; invalid is an error."""
    try:
        port = int(env["PORT"])
        if not 1 <= port <= 65535:
            raise ConfigError(f"PORT out of range: {port}")
        return port
    except Exception:
        return DEFAULT_PORT
