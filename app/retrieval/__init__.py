"""Hybrid code retrieval: AST-aware chunks, keyword + vector + graph search, RRF fusion.

Indexing: ``indexing.RepositoryIndexer``. Retrieval: ``service.RetrievalService``.
Each strategy (``keyword_search``, ``vector_search``, ``graph_search``) is usable
on its own. Nothing here calls an LLM or executes repository code.
"""
