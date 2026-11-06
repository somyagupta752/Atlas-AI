"""Neo4j driver lifecycle. Credentials come only from settings and are never logged."""

import logging
from dataclasses import dataclass

from neo4j import Driver, GraphDatabase
from neo4j.exceptions import DriverError, Neo4jError
from pydantic import SecretStr

from app.config import Settings
from app.knowledge_graph.repository import GraphStoreError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Neo4jConfig:
    uri: str
    username: str
    password: SecretStr  # repr() shows '**********'
    database: str

    @classmethod
    def from_settings(cls, settings: Settings) -> "Neo4jConfig | None":
        """Return None when Neo4j is not configured, so the rest of the app still runs."""
        if not (settings.neo4j_uri and settings.neo4j_username and settings.neo4j_password):
            return None
        return cls(
            uri=settings.neo4j_uri,
            username=settings.neo4j_username,
            password=settings.neo4j_password,
            database=settings.neo4j_database,
        )


def create_driver(config: Neo4jConfig) -> Driver:
    """Create a pooled, thread-safe driver. No connection is opened yet."""
    return GraphDatabase.driver(
        config.uri, auth=(config.username, config.password.get_secret_value())
    )


def verify_connectivity(driver: Driver) -> None:
    """Raise ``GraphStoreError`` if the server is unreachable or rejects the credentials."""
    try:
        driver.verify_connectivity()
    except (DriverError, Neo4jError) as exc:
        # Only the exception type is surfaced: messages can include server addresses.
        raise GraphStoreError(f"Neo4j is unavailable ({type(exc).__name__})") from exc
