"""A deliberately small, strict unified-diff parser and a canonical renderer.

Supported (v1):

- normal unified diffs, one or more files, ``--- a/x`` / ``+++ b/x`` headers
  (with or without the ``a/`` and ``b/`` prefixes, with or without a
  ``diff --git`` line and an ``index`` line);
- new files (``--- /dev/null``) and deleted files (``+++ /dev/null``). The
  validator decides whether they are allowed;
- ``\\ No newline at end of file`` markers.

Rejected:

- renames, copies and mode changes;
- binary patches (``GIT binary patch``, ``Binary files ... differ``, NUL bytes);
- quoted paths, lines outside hunks, malformed hunk headers or lines,
  hunks without changes, overlapping hunks.

Hunk line counts are recomputed from the hunk body (like ``git apply
--recount``): models often miscount, and the body is what the patch means.
A corrected count is a warning, not an error.

The parser only parses. Path safety, scope and limits belong to the validator.
Application belongs to ``git apply``, which receives the canonical rendering
of the parsed diff, never the model's raw text.
"""

import re
from dataclasses import dataclass, field
from typing import Literal

DEV_NULL = "/dev/null"
HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?: .*)?$")
NO_NEWLINE = "\\ No newline at end of file"
MAX_ERRORS = 25

# Extended git header lines. Allowed ones carry no meaning for the change itself.
ALLOWED_GIT_HEADERS = ("index ", "new file mode 100644", "deleted file mode 100644")
UNSUPPORTED_GIT_HEADERS = {
    "rename from": "renames are not supported",
    "rename to": "renames are not supported",
    "similarity index": "renames and copies are not supported",
    "dissimilarity index": "rewrites are not supported",
    "copy from": "copies are not supported",
    "copy to": "copies are not supported",
    "old mode": "file mode changes are not supported",
    "new mode": "file mode changes are not supported",
    "new file mode": "new files must use mode 100644",
    "deleted file mode": "deleted files must use mode 100644",
    "GIT binary patch": "binary patches are not supported",
    "Binary files": "binary patches are not supported",
}


class DiffParseError(ValueError):
    def __init__(self, errors: list[str], warnings: list[str] | None = None) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors
        self.warnings = warnings or []


@dataclass(frozen=True)
class HunkLine:
    kind: Literal["context", "added", "removed"]
    text: str
    old_line: int | None  # line number in the original file
    new_line: int | None  # line number in the patched file
    no_newline: bool = False  # followed by "\ No newline at end of file"


@dataclass(frozen=True)
class Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: tuple[HunkLine, ...]

    @property
    def added(self) -> int:
        return sum(1 for line in self.lines if line.kind == "added")

    @property
    def removed(self) -> int:
        return sum(1 for line in self.lines if line.kind == "removed")

    @property
    def old_block(self) -> list[str]:
        return [line.text for line in self.lines if line.kind != "added"]

    @property
    def new_block(self) -> list[str]:
        return [line.text for line in self.lines if line.kind != "removed"]

    @property
    def is_noop(self) -> bool:
        return self.old_block == self.new_block


@dataclass(frozen=True)
class FilePatch:
    old_path: str | None  # None for a new file
    new_path: str | None  # None for a deleted file
    hunks: tuple[Hunk, ...]

    @property
    def operation(self) -> Literal["modify", "add", "delete"]:
        if self.old_path is None:
            return "add"
        if self.new_path is None:
            return "delete"
        return "modify"

    @property
    def path(self) -> str:
        return self.new_path or self.old_path or ""

    @property
    def added(self) -> int:
        return sum(h.added for h in self.hunks)

    @property
    def removed(self) -> int:
        return sum(h.removed for h in self.hunks)


@dataclass(frozen=True)
class ParsedDiff:
    files: tuple[FilePatch, ...]
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def paths(self) -> list[str]:
        return [f.path for f in self.files]

    @property
    def added(self) -> int:
        return sum(f.added for f in self.files)

    @property
    def removed(self) -> int:
        return sum(f.removed for f in self.files)


def _header_path(line: str, marker: str) -> str:
    """The path from a ``---``/``+++`` line, without a trailing tab and timestamp."""
    raw = line[len(marker) :]
    return raw.split("\t", 1)[0].rstrip()


def _strip_prefixes(old: str, new: str) -> tuple[str | None, str | None]:
    """Remove git's ``a/`` and ``b/`` prefixes when both sides use them."""
    old_ok = old == DEV_NULL or old.startswith("a/")
    new_ok = new == DEV_NULL or new.startswith("b/")
    if old_ok and new_ok and not (old == DEV_NULL and new == DEV_NULL):
        old = old if old == DEV_NULL else old[2:]
        new = new if new == DEV_NULL else new[2:]
    return (None if old == DEV_NULL else old), (None if new == DEV_NULL else new)


def _is_file_header(lines: list[str], index: int) -> bool:
    return (
        lines[index].startswith("--- ")
        and index + 1 < len(lines)
        and lines[index + 1].startswith("+++ ")
    )


def _is_boundary(lines: list[str], index: int) -> bool:
    line = lines[index]
    return line.startswith(("@@ ", "diff --git ")) or _is_file_header(lines, index)


def parse_unified_diff(text: str) -> ParsedDiff:
    """Parse ``text``. Raises ``DiffParseError`` with every problem found."""
    errors: list[str] = []
    warnings: list[str] = []

    def error(message: str) -> None:
        if len(errors) < MAX_ERRORS:
            errors.append(message)

    if not isinstance(text, str) or not text.strip():
        raise DiffParseError(["The patch is empty"])
    if "\x00" in text:
        raise DiffParseError(["The patch contains NUL bytes: binary patches are not supported"])
    if "\r\n" in text:
        text = text.replace("\r\n", "\n")
        warnings.append("CRLF line endings in the patch were normalised to LF")
    if "\r" in text:
        raise DiffParseError(["The patch contains carriage returns inside lines"])

    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()

    files: list[FilePatch] = []
    git_paths: tuple[str, str] | None = None
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith("diff --git "):
            parts = line[len("diff --git ") :].split(" ")
            git_paths = (parts[0], parts[-1]) if len(parts) == 2 else None
            if git_paths is None:
                error(f"Line {index + 1}: unsupported 'diff --git' header (quoted or spaced paths)")
            index += 1
            continue
        unsupported = next((reason for prefix, reason in UNSUPPORTED_GIT_HEADERS.items() if line.startswith(prefix)), None)
        if line.startswith(ALLOWED_GIT_HEADERS):
            index += 1
            continue
        if unsupported:
            error(f"Line {index + 1}: {unsupported}")
            index += 1
            continue
        if _is_file_header(lines, index):
            old_raw = _header_path(line, "--- ")
            new_raw = _header_path(lines[index + 1], "+++ ")
            index += 2
            file_patch, index = _parse_file(lines, index, old_raw, new_raw, git_paths, error, warnings)
            git_paths = None
            if file_patch is not None:
                files.append(file_patch)
            continue
        if line.startswith("@@"):
            error(f"Line {index + 1}: hunk without a '---'/'+++' file header")
        elif line.strip():
            error(f"Line {index + 1}: unexpected text outside a hunk")
        index += 1

    if not files and not errors:
        errors.append("The patch contains no file changes")
    if errors:
        raise DiffParseError(errors, warnings)
    return ParsedDiff(files=tuple(files), warnings=tuple(warnings))


def _parse_file(lines, index, old_raw, new_raw, git_paths, error, warnings):
    if old_raw.startswith('"') or new_raw.startswith('"'):
        error(f"Quoted file paths are not supported: {old_raw}")
    old_path, new_path = _strip_prefixes(old_raw, new_raw)
    if old_path is None and new_path is None:
        error("A file header names /dev/null on both sides")
    elif old_path is not None and new_path is not None and old_path != new_path:
        error(f"Renames are not supported: {old_path} -> {new_path}")
    if git_paths is not None:
        expected = {p.removeprefix("a/").removeprefix("b/") for p in git_paths}
        named = {p for p in (old_path, new_path) if p is not None}
        if not named <= expected:
            error(f"The 'diff --git' header does not match the file header for {new_path or old_path}")

    path = new_path or old_path or "?"
    hunks: list[Hunk] = []
    while index < len(lines) and lines[index].startswith("@@"):
        hunk, index = _parse_hunk(lines, index, path, error, warnings)
        if hunk is not None:
            hunks.append(hunk)

    if not hunks:
        error(f"{path}: no hunks (mode-only or empty changes are not supported)")
        return None, index
    previous_end = 0
    for hunk in hunks:
        if hunk.old_start < previous_end:
            error(f"{path}: hunks overlap or are out of order")
        previous_end = hunk.old_start + hunk.old_count
    if old_path is None and any(h.old_count for h in hunks):
        error(f"{path}: a new file can only contain added lines")
    if new_path is None and any(h.new_count for h in hunks):
        error(f"{path}: a deleted file can only contain removed lines")
    return FilePatch(old_path=old_path, new_path=new_path, hunks=tuple(hunks)), index


def _parse_hunk(lines, index, path, error, warnings):
    header = lines[index]
    match = HUNK_HEADER.match(header)
    if not match:
        error(f"{path}: malformed hunk header {header[:80]!r}")
        index += 1
        while index < len(lines) and not _is_boundary(lines, index):
            index += 1
        return None, index
    old_start, old_count, new_start, new_count = (
        int(match.group(1)),
        int(match.group(2) or 1),
        int(match.group(3)),
        int(match.group(4) or 1),
    )
    index += 1
    body: list[str] = []
    while index < len(lines) and not _is_boundary(lines, index):
        body.append(lines[index])
        index += 1
    # Blank separator lines after the last hunk line belong to no hunk.
    while body and body[-1] == "" and _counts(body[:-1]) >= (old_count, new_count):
        body.pop()

    parsed: list[HunkLine] = []
    old_line, new_line = old_start, new_start
    blank_context = False
    for offset, raw in enumerate(body):
        if raw == NO_NEWLINE:
            if not parsed:
                error(f"{path}: 'No newline' marker before any hunk line")
                continue
            last = parsed[-1]
            parsed[-1] = HunkLine(last.kind, last.text, last.old_line, last.new_line, no_newline=True)
            continue
        marker, content = (raw[:1], raw[1:]) if raw else (" ", "")
        if raw == "":
            blank_context = True
        if marker == " ":
            parsed.append(HunkLine("context", content, old_line, new_line))
            old_line += 1
            new_line += 1
        elif marker == "-":
            parsed.append(HunkLine("removed", content, old_line, None))
            old_line += 1
        elif marker == "+":
            parsed.append(HunkLine("added", content, None, new_line))
            new_line += 1
        else:
            error(f"{path}: malformed hunk line {offset + 1} after {header[:40]!r}: lines must start with ' ', '+' or '-'")
    if blank_context:
        warnings.append(f"{path}: empty lines inside a hunk were read as blank context lines")

    real_old = sum(1 for line in parsed if line.kind != "added")
    real_new = sum(1 for line in parsed if line.kind != "removed")
    if (real_old, real_new) != (old_count, new_count):
        warnings.append(
            f"{path}: hunk header said -{old_start},{old_count} +{new_start},{new_count}; "
            f"the body has {real_old} old and {real_new} new lines (counts corrected)"
        )
    if not any(line.kind != "context" for line in parsed):
        error(f"{path}: hunk at line {old_start} contains no added or removed lines")
        return None, index
    # Line numbers are 1-based; only an empty side may start at 0.
    if (real_old and old_start == 0) or (real_new and new_start == 0):
        error(f"{path}: malformed hunk header {header[:80]!r}: line 0 with a non-empty range")
    return Hunk(old_start, real_old, new_start, real_new, tuple(parsed)), index


def _counts(body: list[str]) -> tuple[int, int]:
    old = new = 0
    for raw in body:
        marker = raw[:1] if raw else " "
        if raw == NO_NEWLINE:
            continue
        if marker in (" ", "-"):
            old += 1
        if marker in (" ", "+"):
            new += 1
    return old, new


def render_unified_diff(parsed: ParsedDiff) -> str:
    """The canonical text for ``git apply -p1``: fixed prefixes, recomputed counts, no section text."""
    out: list[str] = []
    for file_patch in parsed.files:
        out.append(f"--- a/{file_patch.old_path}" if file_patch.old_path else f"--- {DEV_NULL}")
        out.append(f"+++ b/{file_patch.new_path}" if file_patch.new_path else f"+++ {DEV_NULL}")
        for hunk in file_patch.hunks:
            out.append(f"@@ -{_range(hunk.old_start, hunk.old_count)} +{_range(hunk.new_start, hunk.new_count)} @@")
            for line in hunk.lines:
                out.append({"context": " ", "added": "+", "removed": "-"}[line.kind] + line.text)
                if line.no_newline:
                    out.append(NO_NEWLINE)
    return "\n".join(out) + "\n"


def _range(start: int, count: int) -> str:
    return f"{start},{count}"


# --- context anchoring ---------------------------------------------------------------
#
# ``git apply`` and ``patch`` read a hunk with no trailing context as anchored to
# the end of the file (and one with no leading context as anchored to the start).
# Models often end a hunk on the changed line, which then fails to apply anywhere
# but at EOF. The validator therefore locates each hunk in the real source (by
# its old lines, nearest to the stated line number) and pads it with up to
# ``CONTEXT_LINES`` lines of real context on each side. Nothing the model wrote
# changes: only unchanged source lines are added, and line numbers are corrected.
# A hunk whose old lines are not in the file is left alone, so application
# reports the conflict.

CONTEXT_LINES = 3


def _locate(source: list[str], block: list[str], hint: int, lower: int) -> int | None:
    if not block:
        return min(max(hint, lower), len(source))
    size = len(block)
    candidates = [i for i in range(lower, len(source) - size + 1) if source[i : i + size] == block]
    return min(candidates, key=lambda i: abs(i - hint)) if candidates else None


def _edge_context(lines: tuple[HunkLine, ...]) -> tuple[int, int]:
    lead = next((i for i, line in enumerate(lines) if line.kind != "context"), len(lines))
    trail = next((i for i, line in enumerate(reversed(lines)) if line.kind != "context"), len(lines))
    return lead, trail


def _renumber(lines: list[HunkLine], old_start: int, new_start: int) -> tuple[HunkLine, ...]:
    out = []
    old, new = old_start, new_start
    for line in lines:
        if line.kind == "context":
            out.append(HunkLine("context", line.text, old, new, line.no_newline))
            old, new = old + 1, new + 1
        elif line.kind == "removed":
            out.append(HunkLine("removed", line.text, old, None, line.no_newline))
            old += 1
        else:
            out.append(HunkLine("added", line.text, None, new, line.no_newline))
            new += 1
    return tuple(out)


def anchor_context(
    parsed: ParsedDiff, sources: dict[str, str], context: int = CONTEXT_LINES
) -> tuple[ParsedDiff, list[str]]:
    """Pad modified files' hunks with real context from ``sources`` (path -> text)."""
    notes: list[str] = []
    files: list[FilePatch] = []
    for file_patch in parsed.files:
        text = sources.get(file_patch.path)
        if file_patch.operation != "modify" or text is None:
            files.append(file_patch)
            continue
        source = text.split("\n")
        ends_with_newline = bool(source) and source[-1] == ""
        if ends_with_newline:
            source.pop()
        positions: list[int] = []
        lower = 0
        for hunk in file_patch.hunks:
            hint = hunk.old_start - 1 if hunk.old_count else hunk.old_start
            index = _locate(source, hunk.old_block, hint, lower)
            if index is None:
                break
            positions.append(index)
            lower = index + hunk.old_count
        if len(positions) != len(file_patch.hunks):
            files.append(file_patch)  # a hunk does not match: let application report the conflict
            continue

        hunks: list[Hunk] = []
        padded_end = 0
        delta = 0
        for k, (hunk, start) in enumerate(zip(file_patch.hunks, positions, strict=True)):
            end = start + hunk.old_count
            upper = positions[k + 1] if k + 1 < len(positions) else len(source)
            lead, trail = _edge_context(hunk.lines)
            add_lead = max(0, min(context - lead, start - padded_end))
            add_trail = max(0, min(context - trail, upper - end))
            before = [HunkLine("context", source[j], None, None) for j in range(start - add_lead, start)]
            after = [
                HunkLine("context", source[j], None, None,
                         no_newline=(j == len(source) - 1 and not ends_with_newline))
                for j in range(end, end + add_trail)
            ]
            if add_lead or add_trail:
                notes.append(f"{file_patch.path}: added {add_lead + add_trail} context line(s) from the source "
                             f"to the hunk at line {start + 1}")
            lines = before + list(hunk.lines) + after
            old_count = hunk.old_count + add_lead + add_trail
            new_count = hunk.new_count + add_lead + add_trail
            first = start - add_lead  # 0-based first old line of the padded hunk
            old_start = first + 1 if old_count else first
            new_start = first + 1 + delta if new_count else first + delta
            hunks.append(Hunk(old_start, old_count, new_start, new_count,
                              _renumber(lines, max(old_start, 1), max(new_start, 1))))
            delta += hunk.new_count - hunk.old_count
            padded_end = end + add_trail
        files.append(FilePatch(file_patch.old_path, file_patch.new_path, tuple(hunks)))
    return ParsedDiff(files=tuple(files), warnings=parsed.warnings), notes
