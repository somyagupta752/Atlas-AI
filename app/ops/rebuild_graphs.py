"""Rebuild code graphs from source after a Neo4j loss (Task 15.6).

The graph is derived data. The index registry (``repository_indexes``) remembers each repository's root path, so a lost
or corrupted graph is rebuilt by analysing the source again. Only repositories whose root is still inside the allowed
roots are rebuilt; the rest are reported, never guessed.

    python -m app.ops.rebuild_graphs [REPOSITORY_ID ...]
"""

import sys
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.knowledge_graph.service import GraphService
from app.models import RepositoryIndexRecord


@dataclass
class RebuildReport:
    rebuilt: list[str] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)


def rebuild_graphs(session: Session, service: GraphService, allowed_roots: list[Path],
                   only: list[str] | None = None) -> RebuildReport:
    report = RebuildReport()
    query = select(RepositoryIndexRecord).order_by(RepositoryIndexRecord.repository_id)
    if only:
        query = query.where(RepositoryIndexRecord.repository_id.in_(only))
    roots = [r.expanduser().resolve() for r in allowed_roots]
    for record in session.scalars(query):
        path = Path(record.root_path).expanduser().resolve()
        if not any(path.is_relative_to(root) for root in roots):
            report.skipped[record.repository_id] = "its root is outside the allowed roots"
        elif not path.is_dir():
            report.skipped[record.repository_id] = "its source directory no longer exists"
        else:
            try:
                service.build_graph(path, record.repository_id)
                report.rebuilt.append(record.repository_id)
            except Exception as exc:  # noqa: BLE001 - one broken repository must not stop the rest
                report.skipped[record.repository_id] = f"build failed: {type(exc).__name__}"
    return report


def main(argv: list[str]) -> int:
    from app.config import get_settings
    from app.database import SessionLocal
    from app.knowledge_graph.neo4j_client import Neo4jConfig, create_driver
    from app.knowledge_graph.neo4j_repository import Neo4jGraphRepository

    settings = get_settings()
    config = Neo4jConfig.from_settings(settings)
    if config is None:
        print("Neo4j is not configured")
        return 1
    repository = Neo4jGraphRepository(create_driver(config), config.database)
    repository.ensure_schema()
    with SessionLocal() as session:
        report = rebuild_graphs(session, GraphService(repository), settings.allowed_repository_roots, argv or None)
    print(f"rebuilt: {report.rebuilt}")
    for name, why in report.skipped.items():
        print(f"skipped {name}: {why}")
    return 0 if not report.skipped else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
