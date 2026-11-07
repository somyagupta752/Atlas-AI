from app.tools._graph import CallGraphTool


class GetCalleesTool(CallGraphTool):
    name = "get_callees"
    direction = "callees"
    description = (
        "Finds the functions and methods that a target function calls, using the static code "
        "knowledge graph (CALLS relationships). Returns each callee's qualified name, file, line "
        "range and resolution status, plus 'unresolved_calls': calls the graph could not resolve "
        "(builtins such as len(), external libraries, dynamic expressions), with the reason."
    )
