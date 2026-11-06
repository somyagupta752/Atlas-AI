import textwrap

import pytest

from app.code_intelligence import analyze_repository
from app.knowledge_graph.builder import build_code_graph
from app.retrieval.chunker import ChunkerConfig, chunk_source, split_lines
from app.retrieval.indexing import load_source_files
from app.retrieval.models import ChunkType

USER_SERVICE = textwrap.dedent(
    '''\
    """User service."""
    from app.repo import UserRepository


    class UserService:
        def __init__(self, repository):
            self.repository = repository

        def get_user(self, user_id):
            user = self.repository.find_by_id(user_id)
            return user

        def delete_user(self, user_id):
            self.repository.delete(user_id)


    def build():
        def helper():
            return UserRepository()
        return UserService(helper())


    SERVICE = build()
    '''
)


def by_name(chunks):
    return {(c.entity_type, c.qualified_name): c for c in chunks}


def test_functions_methods_classes_and_nested_functions_become_chunks():
    chunks = by_name(chunk_source("demo", "app/user.py", USER_SERVICE))

    assert (ChunkType.CLASS, "UserService") in chunks
    assert (ChunkType.METHOD, "UserService.get_user") in chunks
    assert (ChunkType.METHOD, "UserService.delete_user") in chunks
    assert (ChunkType.METHOD, "UserService.__init__") in chunks
    assert (ChunkType.FUNCTION, "build") in chunks
    assert (ChunkType.NESTED_FUNCTION, "build.<locals>.helper") in chunks


def test_line_ranges_are_exact_and_content_matches_the_source():
    lines = USER_SERVICE.splitlines()
    chunks = by_name(chunk_source("demo", "app/user.py", USER_SERVICE))

    get_user = chunks[(ChunkType.METHOD, "UserService.get_user")]
    assert (get_user.start_line, get_user.end_line) == (9, 11)
    assert get_user.content == "\n".join(lines[8:11])
    assert get_user.content.lstrip().startswith("def get_user")

    service = chunks[(ChunkType.CLASS, "UserService")]
    assert (service.start_line, service.end_line) == (5, 14)
    for chunk in chunks.values():
        assert chunk.content == "\n".join(lines[chunk.start_line - 1 : chunk.end_line])


def test_class_and_method_chunks_overlap_so_the_class_keeps_context():
    chunks = by_name(chunk_source("demo", "app/user.py", USER_SERVICE))
    service = chunks[(ChunkType.CLASS, "UserService")]
    get_user = chunks[(ChunkType.METHOD, "UserService.get_user")]

    assert service.start_line <= get_user.start_line <= get_user.end_line <= service.end_line
    assert "self.repository = repository" in service.content
    assert "self.repository = repository" not in get_user.content


def test_module_level_code_becomes_module_chunks():
    modules = [c for c in chunk_source("demo", "app/user.py", USER_SERVICE) if c.entity_type == "module"]

    assert [(c.start_line, c.end_line) for c in modules] == [(1, 2), (23, 23)]
    assert "from app.repo import UserRepository" in modules[0].content
    assert modules[1].content == "SERVICE = build()"
    assert modules[0].entity_id == "file:demo:app/user.py"
    assert modules[0].id == "chunk:file:demo:app/user.py"
    assert modules[1].id == "chunk:file:demo:app/user.py#part2"


def test_chunks_carry_repository_file_and_entity_identity():
    chunks = by_name(chunk_source("demo", "app/user.py", USER_SERVICE))
    get_user = chunks[(ChunkType.METHOD, "UserService.get_user")]

    assert get_user.repository_id == "demo"
    assert get_user.file_path == "app/user.py"
    assert get_user.entity_id == "function:demo:app/user.py::UserService.get_user"
    assert get_user.id == "chunk:function:demo:app/user.py::UserService.get_user"
    assert get_user.metadata["module"] == "app.user"
    assert get_user.metadata["parent_class"] == "UserService"
    assert get_user.metadata["signature"] == "def get_user(self, user_id):"
    assert get_user.dotted_name == "app.user.UserService.get_user"


def test_chunk_ids_are_stable_and_unique():
    first = chunk_source("demo", "app/user.py", USER_SERVICE)
    second = chunk_source("demo", "app/user.py", USER_SERVICE)

    assert [c.id for c in first] == [c.id for c in second]
    assert len({c.id for c in first}) == len(first)
    assert [c.content_hash for c in first] == [c.content_hash for c in second]


def test_same_names_in_two_repositories_get_different_ids():
    a = chunk_source("repo-a", "app/user.py", USER_SERVICE)
    b = chunk_source("repo-b", "app/user.py", USER_SERVICE)

    assert not {c.id for c in a} & {c.id for c in b}


def test_entity_ids_match_the_knowledge_graph(write_repo):
    root = write_repo(
        {
            "app/__init__.py": "",
            "app/user.py": USER_SERVICE,
            "app/props.py": """
                class Box:
                    @property
                    def size(self):
                        return 1

                    @size.setter
                    def size(self, value):
                        pass
                """,
        }
    )
    from app.retrieval.chunker import chunk_repository

    files, _ = load_source_files(root)
    chunks = chunk_repository("demo", files)
    graph = build_code_graph(analyze_repository(root), "demo")
    graph_ids = {node.id for node in graph.nodes}

    assert {c.entity_id for c in chunks} <= graph_ids
    setters = [c for c in chunks if c.qualified_name == "Box.size"]
    assert len(setters) == 2
    assert all("@L" in c.entity_id for c in setters)  # redefinitions stay distinct


def test_decorators_stay_with_the_function_and_are_in_metadata():
    source = textwrap.dedent(
        """\
        from fastapi import FastAPI

        app = FastAPI()


        @app.get(
            "/users/{user_id}"
        )
        def read_user(user_id: int):
            return user_id
        """
    )
    chunk = by_name(chunk_source("demo", "app/api.py", source))[(ChunkType.FUNCTION, "read_user")]

    assert chunk.start_line == 6
    assert chunk.metadata["definition_line"] == 9
    assert chunk.content.startswith("@app.get(")
    assert chunk.metadata["decorators"] == ["app.get('/users/{user_id}')"]


def test_large_function_is_split_into_windows_that_cover_it_exactly():
    body = "\n".join(f"    value_{i} = {i}" for i in range(25))
    source = f"def big():\n{body}\n    return value_0\n"
    chunks = chunk_source("demo", "big.py", source, ChunkerConfig(max_lines=10))

    parts = [c for c in chunks if c.qualified_name == "big"]
    assert len(parts) == 3
    assert [(c.start_line, c.end_line) for c in parts] == [(1, 10), (11, 20), (21, 27)]
    assert parts[0].id == "chunk:function:demo:big.py::big"
    assert parts[1].id == "chunk:function:demo:big.py::big#part2"
    assert all(c.metadata["total_parts"] == 3 for c in parts)
    assert all(c.metadata["signature"] == "def big():" for c in parts)
    assert "\n".join(c.content for c in parts) == source.rstrip("\n")


def test_window_respects_the_character_limit():
    source = "def wide():\n" + "\n".join(f"    x = '{'y' * 50}'" for _ in range(6)) + "\n"
    chunks = chunk_source("demo", "wide.py", source, ChunkerConfig(max_lines=100, max_chars=150))

    assert all(len(c.content) <= 150 for c in chunks)
    assert len(chunks) > 1


def test_large_class_becomes_a_header_chunk_and_methods_stay_whole():
    methods = "\n\n".join(f"    def m{i}(self):\n        return {i}" for i in range(10))
    source = f'class Big:\n    """Doc."""\n    LIMIT = 3\n\n{methods}\n'
    chunks = by_name(chunk_source("demo", "big.py", source, ChunkerConfig(max_lines=12)))

    header = chunks[(ChunkType.CLASS, "Big")]
    assert (header.start_line, header.end_line) == (1, 3)
    assert header.metadata["truncated"] is True
    assert header.metadata["methods"] == [f"m{i}" for i in range(10)]
    assert (ChunkType.METHOD, "Big.m9") in chunks


@pytest.mark.parametrize("source", ["", "\n\n   \n"])
def test_empty_files_have_no_chunks(source):
    assert chunk_source("demo", "empty.py", source) == []


def test_syntax_errors_still_produce_searchable_module_chunks():
    source = "def broken(:\n    raise ZeroDivisionError('boom')\n"
    chunks = chunk_source("demo", "broken.py", source)

    assert len(chunks) == 1
    chunk = chunks[0]
    assert chunk.entity_type == ChunkType.MODULE
    assert chunk.metadata["parse_status"] == "error"
    assert chunk.metadata["parse_errors"]
    assert "ZeroDivisionError" in chunk.content
    assert (chunk.start_line, chunk.end_line) == (1, 2)


def test_crlf_line_endings_keep_line_numbers_aligned():
    source = "import os\r\n\r\ndef f():\r\n    return 1\r\n"
    chunk = by_name(chunk_source("demo", "crlf.py", source))[(ChunkType.FUNCTION, "f")]

    assert (chunk.start_line, chunk.end_line) == (3, 4)
    assert chunk.content == "def f():\n    return 1"
    assert split_lines(source) == ["import os", "", "def f():", "    return 1"]


def test_invalid_chunker_limits_are_rejected():
    with pytest.raises(ValueError):
        ChunkerConfig(max_lines=0)
