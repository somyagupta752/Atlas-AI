"""The AI debugging agent (Task 12).

Boundaries, one module each:

- ``llm.py``: provider-independent LLM interface, the Anthropic adapter and a
  scripted fake for tests. Provider objects never leave this module.
- ``state.py``: what one investigation remembers (messages, observations).
- ``tool_registry.py`` / ``tool_executor.py``: which tools exist, and how an
  untrusted model-requested call becomes a validated, bounded execution.
- ``loop.py``: the agent loop and its stopping conditions.
- ``prompts.py``: the system prompt and the fixed follow-up messages.
- ``hypotheses.py`` / ``evidence.py`` / ``diagnosis.py``: turn the model's
  final answer into a validated, evidence-grounded ``RootCauseAnalysis``.
- ``investigator.py``: loop, then diagnosis, repair and finalization.
- ``agent.py``: the service: validate the repository, persist, run, return.
"""
