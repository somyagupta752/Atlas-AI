import ast

import pytest

from app.code_intelligence.parser import (
    SourceParseError,
    SourceReadError,
    parse_source,
    read_source,
)
from app.code_intelligence.schemas import ErrorType


def test_valid_source_returns_module_with_function_location():
    source = "def calculate_total(price, quantity):\n    return price * quantity\n"

    tree = parse_source(source, "pricing.py")

    assert isinstance(tree, ast.Module)
    function = tree.body[0]
    assert isinstance(function, ast.FunctionDef)
    assert function.name == "calculate_total"
    assert (function.lineno, function.end_lineno) == (1, 2)


def test_empty_source_parses_to_empty_module():
    tree = parse_source("", "empty.py")

    assert isinstance(tree, ast.Module)
    assert tree.body == []


def test_syntax_error_keeps_filename_and_location():
    source = "x = 1\ndef broken(:\n    pass\n"

    with pytest.raises(SourceParseError) as excinfo:
        parse_source(source, "pkg/broken.py")

    error = excinfo.value.to_error()
    assert error.error_type == ErrorType.SYNTAX_ERROR
    assert error.file_path == "pkg/broken.py"
    assert error.line == 2
    assert error.column is not None
    assert error.message


def test_indentation_error_is_a_syntax_error():
    with pytest.raises(SourceParseError) as excinfo:
        parse_source("def f():\nreturn 1\n", "indent.py")

    assert excinfo.value.line == 2


def test_null_bytes_are_reported_as_parse_errors():
    with pytest.raises(SourceParseError):
        parse_source("x = 1\x00\n", "nul.py")


def test_syntax_warnings_are_not_printed(recwarn):
    parse_source('pattern = "\\d+"\n', "warn.py")

    assert len(recwarn) == 0


def test_read_source_returns_text(tmp_path):
    path = tmp_path / "ok.py"
    path.write_text("value = 'héllo'\n", encoding="utf-8")

    assert read_source(path) == "value = 'héllo'\n"


def test_read_source_honours_coding_declaration(tmp_path):
    path = tmp_path / "latin.py"
    path.write_bytes("# -*- coding: latin-1 -*-\nname = 'café'\n".encode("latin-1"))

    assert "café" in read_source(path)


def test_missing_file_is_a_read_error_not_a_parse_error(tmp_path):
    with pytest.raises(SourceReadError) as excinfo:
        read_source(tmp_path / "gone.py", display_name="gone.py")

    assert excinfo.value.error_type == ErrorType.READ_ERROR
    assert excinfo.value.file_path == "gone.py"


def test_undecodable_bytes_are_a_decode_error(tmp_path):
    path = tmp_path / "binary.py"
    path.write_bytes(b"x = '\xff\xfe'\n")

    with pytest.raises(SourceReadError) as excinfo:
        read_source(path)

    assert excinfo.value.error_type == ErrorType.DECODE_ERROR


def test_oversized_file_is_rejected(tmp_path):
    path = tmp_path / "huge.py"
    path.write_text("x = 1\n" * 100)

    with pytest.raises(SourceReadError, match="size limit"):
        read_source(path, max_bytes=50)
