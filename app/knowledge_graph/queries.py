"""Reusable, parameterized Cypher.

Values always travel as parameters. Neo4j cannot parameterize labels or
relationship types, so those queries are generated once, at import time, from
the ``NodeLabel`` and ``RelationshipType`` enums. User input never reaches a
query string.
"""

from app.knowledge_graph.models import NodeLabel, RelationshipType

# --- schema -----------------------------------------------------------------

SCHEMA_STATEMENTS: tuple[str, ...] = (
    "CREATE CONSTRAINT code_entity_id IF NOT EXISTS FOR (n:CodeEntity) REQUIRE n.id IS UNIQUE",
    (
        "CREATE CONSTRAINT unresolved_reference_id IF NOT EXISTS "
        "FOR (u:UnresolvedReference) REQUIRE u.id IS UNIQUE"
    ),
    "CREATE INDEX code_entity_repository IF NOT EXISTS FOR (n:CodeEntity) ON (n.repository_id)",
    (
        "CREATE INDEX unresolved_reference_repository IF NOT EXISTS "
        "FOR (u:UnresolvedReference) ON (u.repository_id)"
    ),
    (
        "CREATE INDEX class_qualified_name IF NOT EXISTS "
        "FOR (c:Class) ON (c.repository_id, c.qualified_name)"
    ),
)

# --- writes -----------------------------------------------------------------

# Taking a write lock on the repository node first serializes concurrent saves.
LOCK_REPOSITORY = """
MERGE (r:CodeEntity {id: $repository_node_id})
SET r.build_id = $build_id
"""

UPSERT_NODES: dict[NodeLabel, str] = {
    label: f"""
UNWIND $nodes AS node
MERGE (n:CodeEntity {{id: node.id}})
SET n = node.properties
SET n.id = node.id, n.build_id = $build_id
SET n:{label.value}
"""
    for label in NodeLabel
}

UPSERT_RELATIONSHIPS: dict[RelationshipType, str] = {
    rel_type: f"""
UNWIND $relationships AS rel
MATCH (source:CodeEntity {{id: rel.source_id}})
MATCH (target:CodeEntity {{id: rel.target_id}})
MERGE (source)-[r:{rel_type.value} {{id: rel.id}}]->(target)
SET r = rel.properties
SET r.id = rel.id, r.build_id = $build_id
RETURN count(r) AS written
"""
    for rel_type in RelationshipType
}

UPSERT_UNRESOLVED = """
UNWIND $references AS ref
MERGE (u:UnresolvedReference {id: ref.id})
SET u = ref.properties
SET u.id = ref.id, u.repository_id = $repository_id, u.build_id = $build_id
"""

DELETE_STALE_RELATIONSHIPS = """
MATCH (:CodeEntity {repository_id: $repository_id})-[r]->()
WHERE r.build_id IS NULL OR r.build_id <> $build_id
DELETE r
"""

DELETE_STALE_NODES = """
MATCH (n:CodeEntity {repository_id: $repository_id})
WHERE n.build_id IS NULL OR n.build_id <> $build_id
DETACH DELETE n
"""

DELETE_STALE_UNRESOLVED = """
MATCH (u:UnresolvedReference {repository_id: $repository_id})
WHERE u.build_id IS NULL OR u.build_id <> $build_id
DELETE u
"""

DELETE_GRAPH_NODES = """
MATCH (n:CodeEntity {repository_id: $repository_id})
DETACH DELETE n
"""

DELETE_GRAPH_UNRESOLVED = """
MATCH (u:UnresolvedReference {repository_id: $repository_id})
DELETE u
"""

# --- reads ------------------------------------------------------------------

GET_REPOSITORY_ROOT = """
MATCH (r:Repository:CodeEntity {id: $repository_node_id})
RETURN r.root_path AS root_path
"""

GET_NODES = """
MATCH (n:CodeEntity {repository_id: $repository_id})
RETURN n.id AS id,
       [label IN labels(n) WHERE label <> 'CodeEntity'] AS labels,
       properties(n) AS properties
ORDER BY id
"""

GET_RELATIONSHIPS = """
MATCH (source:CodeEntity {repository_id: $repository_id})-[r]->(target:CodeEntity)
RETURN r.id AS id, type(r) AS type, source.id AS source_id, target.id AS target_id,
       properties(r) AS properties
ORDER BY id
"""

GET_UNRESOLVED = """
MATCH (u:UnresolvedReference {repository_id: $repository_id})
RETURN properties(u) AS properties
ORDER BY u.id
"""

COUNT_NODES_BY_LABEL = """
MATCH (n:CodeEntity {repository_id: $repository_id})
UNWIND [label IN labels(n) WHERE label <> 'CodeEntity'] AS label
RETURN label, count(*) AS count
ORDER BY label
"""

COUNT_RELATIONSHIPS_BY_TYPE = """
MATCH (:CodeEntity {repository_id: $repository_id})-[r]->()
RETURN type(r) AS type, count(*) AS count
ORDER BY type
"""

COUNT_UNRESOLVED_BY_REASON = """
MATCH (u:UnresolvedReference {repository_id: $repository_id})
RETURN u.reason AS reason, count(*) AS count
ORDER BY reason
"""

# --- code questions ---------------------------------------------------------

CLASSES_IN_REPOSITORY = """
MATCH (r:Repository {id: $repository_node_id})-[:CONTAINS]->(f:File)-[:CONTAINS]->(c:Class)
RETURN c AS node
ORDER BY c.id
"""

METHODS_OF_CLASS = """
MATCH (c:Class {repository_id: $repository_id, qualified_name: $qualified_name})
      -[:HAS_METHOD]->(m:Function)
RETURN m AS node
ORDER BY m.id
"""

CALLERS_OF_FUNCTION = """
MATCH (caller:Function)-[:CALLS]->(target:Function {id: $function_id})
RETURN caller AS node
ORDER BY caller.id
"""

FILES_IMPORTING_FILE = """
MATCH (source:File)-[:IMPORTS]->(target:File {id: $file_id})
RETURN DISTINCT source AS node
ORDER BY source.id
"""

SUBCLASSES_OF_CLASS = """
MATCH (child:Class)-[:EXTENDS]->(parent:Class {id: $class_id})
RETURN child AS node
ORDER BY child.id
"""
