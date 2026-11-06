"""Code knowledge graph: Task 9 analysis -> validated graph -> pluggable storage.

``models``, ``resolver``, ``builder`` and ``validator`` have no storage
dependencies. ``neo4j_client`` and ``neo4j_repository`` are the only modules
that import the Neo4j driver.
"""
