from app.tools._graph import CallGraphTool


class GetCallersTool(CallGraphTool):
    name = "get_callers"
    direction = "callers"
    description = (
        "Finds the functions and methods that call a target function, using the static code "
        "knowledge graph (CALLS relationships). Returns each caller's qualified name, file, line "
        "range and whether the edge is 'resolved' or 'inferred'. Use it to trace who passes data "
        "into, or depends on the result of, a suspect function."
    )
