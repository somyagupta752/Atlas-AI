import pytest

from app.patching.diff_parser import (
    DiffParseError,
    anchor_context,
    parse_unified_diff,
    render_unified_diff,
)
from tests.patching.conftest import AVERAGE_FIX

TWO_FILES = """\
diff --git a/pkg/a.py b/pkg/a.py
index 1111111..2222222 100644
--- a/pkg/a.py
+++ b/pkg/a.py
@@ -1,3 +1,3 @@ def f():
 x = 1
-y = 2
+y = 3
 z = 4
diff --git a/pkg/b.py b/pkg/b.py
--- a/pkg/b.py
+++ b/pkg/b.py
@@ -10,2 +10,3 @@
 a
+b
 c
@@ -20,2 +21,1 @@
 d
-e
"""


def errors_of(text: str) -> list[str]:
    with pytest.raises(DiffParseError) as info:
        parse_unified_diff(text)
    return info.value.errors


def test_parses_paths_hunks_and_lines():
    parsed = parse_unified_diff(AVERAGE_FIX)
    (file_patch,) = parsed.files
    assert (file_patch.old_path, file_patch.new_path, file_patch.operation) == ("solution.py", "solution.py", "modify")
    (hunk,) = file_patch.hunks
    assert (hunk.old_start, hunk.old_count, hunk.new_start, hunk.new_count) == (1, 2, 1, 4)
    kinds = [line.kind for line in hunk.lines]
    assert kinds == ["context", "added", "added", "context"]
    assert hunk.lines[1].text == "    if not numbers:" and hunk.lines[1].new_line == 2
    assert hunk.lines[3].old_line == 2 and hunk.lines[3].new_line == 4
    assert (parsed.added, parsed.removed) == (2, 0)
    assert not parsed.warnings


def test_multiple_files_and_hunks_with_git_headers():
    parsed = parse_unified_diff(TWO_FILES)
    assert parsed.paths == ["pkg/a.py", "pkg/b.py"]
    assert [len(f.hunks) for f in parsed.files] == [1, 2]
    assert (parsed.added, parsed.removed) == (2, 2)


def test_paths_without_prefixes_and_timestamps():
    diff = "--- solution.py\t2026-01-01 00:00:00\n+++ solution.py\t2026-01-01 00:00:01\n@@ -1 +1 @@\n-a\n+b\n"
    assert parse_unified_diff(diff).paths == ["solution.py"]


def test_new_and_deleted_files():
    new = parse_unified_diff("--- /dev/null\n+++ b/pkg/new.py\n@@ -0,0 +1,2 @@\n+x = 1\n+y = 2\n").files[0]
    assert new.operation == "add" and new.old_path is None and new.path == "pkg/new.py"
    gone = parse_unified_diff("--- a/old.py\n+++ /dev/null\n@@ -1,1 +0,0 @@\n-x = 1\n").files[0]
    assert gone.operation == "delete" and gone.path == "old.py"


def test_hunk_counts_are_recomputed_with_a_warning():
    diff = AVERAGE_FIX.replace("@@ -1,2 +1,4 @@", "@@ -1,7 +1,9 @@")
    parsed = parse_unified_diff(diff)
    assert (parsed.files[0].hunks[0].old_count, parsed.files[0].hunks[0].new_count) == (2, 4)
    assert any("counts corrected" in w for w in parsed.warnings)


def test_blank_context_lines_and_trailing_separator():
    diff = "--- a/x.py\n+++ b/x.py\n@@ -1,3 +1,3 @@\n a\n\n-b\n+c\n\n"
    hunk = parse_unified_diff(diff).files[0].hunks[0]
    assert [line.kind for line in hunk.lines] == ["context", "context", "removed", "added"]


def test_no_newline_marker_round_trips():
    diff = "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n\\ No newline at end of file\n+b\n\\ No newline at end of file\n"
    parsed = parse_unified_diff(diff)
    assert all(line.no_newline for line in parsed.files[0].hunks[0].lines)
    assert render_unified_diff(parsed) == diff.replace("@@ -1 +1 @@", "@@ -1,1 +1,1 @@")


def test_canonical_rendering_normalises_headers_and_counts():
    diff = "--- solution.py\n+++ solution.py\n@@ -1,9 +1,9 @@ section text\n def f():\n-    return 1\n+    return 2\n"
    assert render_unified_diff(parse_unified_diff(diff)) == (
        "--- a/solution.py\n+++ b/solution.py\n@@ -1,2 +1,2 @@\n def f():\n-    return 1\n+    return 2\n"
    )


def test_crlf_is_normalised():
    parsed = parse_unified_diff(AVERAGE_FIX.replace("\n", "\r\n"))
    assert parsed.files[0].hunks[0].lines[1].text == "    if not numbers:"
    assert any("CRLF" in w for w in parsed.warnings)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", "empty"),
        ("   \n", "empty"),
        ("just some prose\n", "outside a hunk"),
        ("@@ -1 +1 @@\n-a\n+b\n", "without a '---'/'+++' file header"),
        ("--- a/x.py\n+++ b/x.py\n@@ -1,2 +1,2\n a\n-b\n+c\n", "malformed hunk header"),
        ("--- a/x.py\n+++ b/x.py\n@@ -1,2 +1,2 @@\n a\n*b\n+c\n", "malformed hunk line"),
        ("--- a/x.py\n+++ b/x.py\n@@ -1,2 +1,2 @@\n a\n b\n", "no added or removed lines"),
        ("--- a/x.py\n+++ b/x.py\n", "no hunks"),
        ("--- a/x.py\n+++ b/y.py\n@@ -1 +1 @@\n-a\n+b\n", "Renames are not supported"),
        ("diff --git a/x.py b/y.py\nsimilarity index 90%\nrename from x.py\nrename to y.py\n", "renames"),
        ("diff --git a/x.py b/x.py\nold mode 100644\nnew mode 100755\n", "mode changes"),
        ("diff --git a/x.bin b/x.bin\nGIT binary patch\nliteral 3\nabc\n", "binary"),
        ("Binary files a/x.png and b/x.png differ\n", "binary"),
        ("--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\x00\n", "NUL"),
        ('--- "a/x y.py"\n+++ "b/x y.py"\n@@ -1 +1 @@\n-a\n+b\n', "Quoted"),
        ("--- /dev/null\n+++ /dev/null\n@@ -0,0 +1 @@\n+a\n", "/dev/null on both sides"),
        ("--- a/x.py\n+++ b/x.py\n@@ -5,2 +5,2 @@\n a\n-b\n+c\n@@ -1,2 +1,2 @@\n a\n-b\n+c\n", "overlap"),
        ("--- a/x.py\n+++ b/x.py\n@@ -0,1 +1,1 @@\n-a\n+b\n", "line 0"),
    ],
)
def test_malformed_and_unsupported_diffs_are_rejected(text, expected):
    assert any(expected.lower() in e.lower() for e in errors_of(text)), errors_of(text)


def test_every_problem_is_reported():
    text = "prose\n--- a/x.py\n+++ b/x.py\n@@ -1,2 +1,2 @@\n a\n*b\n"
    assert len(errors_of(text)) >= 2


# --- context anchoring -----------------------------------------------------------------

SOURCE = "".join(f"line {i}\n" for i in range(1, 21))


def anchored(diff, source=SOURCE, path="x.py"):
    parsed, notes = anchor_context(parse_unified_diff(diff), {path: source})
    return parsed.files[0].hunks, notes


def test_hunk_without_trailing_context_is_padded_from_the_source():
    (hunk,), notes = anchored("--- a/x.py\n+++ b/x.py\n@@ -10,1 +10,1 @@\n-line 10\n+changed\n")
    assert hunk.old_block == [f"line {i}" for i in range(7, 14)]
    assert (hunk.old_start, hunk.old_count, hunk.new_start, hunk.new_count) == (7, 7, 7, 7)
    assert notes and "added 6 context line(s)" in notes[0]


def test_wrong_line_numbers_are_corrected_to_the_real_position():
    (hunk,), _ = anchored("--- a/x.py\n+++ b/x.py\n@@ -2,2 +2,3 @@\n line 15\n+new\n line 16\n")
    assert hunk.old_start == 13 and hunk.new_block[3] == "new"


def test_multiple_hunks_do_not_overlap_and_new_starts_shift():
    diff = "--- a/x.py\n+++ b/x.py\n@@ -3 +3,2 @@\n line 3\n+a\n@@ -5 +6 @@\n-line 5\n+b\n"
    (first, second), _ = anchored(diff)
    assert first.old_start + first.old_count <= second.old_start
    assert second.new_start == second.old_start + 1  # the first hunk added one line


def test_unmatched_hunk_is_left_for_the_applier_to_reject():
    diff = "--- a/x.py\n+++ b/x.py\n@@ -3 +3 @@\n-not in the file\n+b\n"
    (hunk,), notes = anchored(diff)
    assert hunk.old_block == ["not in the file"] and not notes


def test_padding_up_to_an_end_without_newline():
    (hunk,), _ = anchored("--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+z\n", source="a\nb\nc")
    assert hunk.lines[-1].text == "c" and hunk.lines[-1].no_newline
    assert render_unified_diff(anchor_context(parse_unified_diff(
        "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+z\n"), {"x.py": "a\nb\nc"})[0]).endswith(
        " c\n\\ No newline at end of file\n")
