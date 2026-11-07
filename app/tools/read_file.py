from pydantic import Field, model_validator

from app.agents.schemas import SourceLocation
from app.tools.base import (
    BaseTool,
    ToolArguments,
    ToolContext,
    ToolInputError,
    ToolOutput,
    read_text,
    redact_secrets,
    resolve_in_repository,
)

MAX_LINES_PER_READ = 400
MAX_LINE_CHARS = 500


class ReadFileArguments(ToolArguments):
    file_path: str = Field(min_length=1, max_length=500, description="Path relative to the repository root.")
    start_line: int | None = Field(default=None, ge=1, description="First line to return (1-based).")
    end_line: int | None = Field(default=None, ge=1, description="Last line to return (inclusive).")

    @model_validator(mode="after")
    def _ordered(self) -> "ReadFileArguments":
        if self.start_line and self.end_line and self.end_line < self.start_line:
            raise ValueError("end_line must be >= start_line")
        return self


class ReadFileTool(BaseTool):
    name = "read_file"
    description = (
        "Reads a text file from the repository under investigation and returns its lines, "
        "each prefixed with its 1-based line number. Optionally limit to start_line..end_line. "
        f"Returns at most {MAX_LINES_PER_READ} lines per call; 'truncated' and 'total_lines' tell "
        "you when to read further. Paths must be relative to the repository root. "
        "Does not execute the file. Files that may hold secrets are refused."
    )
    Arguments = ReadFileArguments

    def execute(self, arguments: ReadFileArguments, context: ToolContext) -> ToolOutput:
        path, relative = resolve_in_repository(context.repository_root, arguments.file_path)
        lines = read_text(path).splitlines()
        total = len(lines)
        if total == 0:
            return ToolOutput(
                data={"file_path": relative, "total_lines": 0, "start_line": None, "end_line": None,
                      "truncated": False, "content": ""},
                locations=[SourceLocation(file_path=relative)],
            )
        start = arguments.start_line or 1
        if start > total:
            raise ToolInputError(f"start_line {start} is beyond the end of the file ({total} lines)")
        requested_end = min(arguments.end_line or total, total)
        budget = context.max_output_bytes * 3 // 4  # leave room for the JSON envelope
        end = start - 1
        rendered: list[str] = []
        size = 0
        for number in range(start, min(requested_end, start + MAX_LINES_PER_READ - 1) + 1):
            text = lines[number - 1]
            if len(text) > MAX_LINE_CHARS:
                text = text[:MAX_LINE_CHARS] + " ...[line truncated]"
            line = f"{number:>5} | {text}"
            size += len(line.encode()) + 1
            if size > budget and rendered:
                break
            rendered.append(line)
            end = number
        return ToolOutput(
            data={
                "file_path": relative,
                "total_lines": total,
                "start_line": start,
                "end_line": end,
                "truncated": end < requested_end,
                "content": redact_secrets("\n".join(rendered)),
            },
            locations=[SourceLocation(file_path=relative, start_line=start, end_line=end)],
        )
