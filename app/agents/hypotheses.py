"""Structured hypothesis tracking. No probabilities: explicit evidence links only.

A hypothesis's status must agree with its evidence:

- ``supported`` needs at least one supporting evidence item;
- ``rejected`` needs at least one contradicting item;
- ``proposed`` and ``inconclusive`` need nothing (but may carry either).

Links can be declared on either side (the hypothesis lists evidence IDs, or the
evidence lists hypothesis IDs). ``HypothesisBoard`` merges both, so each
hypothesis ends up with the complete list.
"""

from app.agents.schemas import Evidence, Hypothesis, HypothesisStatus


class HypothesisError(ValueError):
    pass


class HypothesisBoard:
    def __init__(self) -> None:
        self._hypotheses: dict[str, Hypothesis] = {}

    @property
    def hypotheses(self) -> list[Hypothesis]:
        return list(self._hypotheses.values())

    def get(self, hypothesis_id: str) -> Hypothesis:
        try:
            return self._hypotheses[hypothesis_id]
        except KeyError:
            raise HypothesisError(f"Unknown hypothesis {hypothesis_id!r}") from None

    def propose(self, hypothesis_id: str, description: str) -> Hypothesis:
        if hypothesis_id in self._hypotheses:
            raise HypothesisError(f"Duplicate hypothesis id {hypothesis_id!r}")
        hypothesis = Hypothesis(id=hypothesis_id, description=description)
        self._hypotheses[hypothesis_id] = hypothesis
        return hypothesis

    def add(self, hypothesis: Hypothesis) -> None:
        if hypothesis.id in self._hypotheses:
            raise HypothesisError(f"Duplicate hypothesis id {hypothesis.id!r}")
        self._hypotheses[hypothesis.id] = hypothesis.model_copy(deep=True)

    def link(self, evidence: Evidence) -> None:
        """Attach evidence to every hypothesis it names."""
        for hypothesis_id in evidence.supports_hypothesis_ids:
            _append(self.get(hypothesis_id).supporting_evidence, evidence.id)
        for hypothesis_id in evidence.contradicts_hypothesis_ids:
            _append(self.get(hypothesis_id).contradicting_evidence, evidence.id)

    def set_status(self, hypothesis_id: str, status: HypothesisStatus) -> Hypothesis:
        hypothesis = self.get(hypothesis_id)
        problem = status_problem(hypothesis.model_copy(update={"status": status}))
        if problem:
            raise HypothesisError(problem)
        hypothesis.status = status
        return hypothesis

    def supported(self) -> list[Hypothesis]:
        return [h for h in self._hypotheses.values() if h.status == "supported"]


def status_problem(hypothesis: Hypothesis) -> str | None:
    if hypothesis.status == "supported" and not hypothesis.supporting_evidence:
        return f"Hypothesis {hypothesis.id} is 'supported' but has no supporting evidence"
    if hypothesis.status == "rejected" and not hypothesis.contradicting_evidence:
        return f"Hypothesis {hypothesis.id} is 'rejected' but has no contradicting evidence"
    return None


def reconcile(hypotheses: list[Hypothesis], evidence: list[Evidence]) -> tuple[list[Hypothesis], list[str]]:
    """Merge links from both sides and check consistency. Returns ``(hypotheses, errors)``."""
    errors: list[str] = []
    board = HypothesisBoard()
    for hypothesis in hypotheses:
        try:
            board.add(hypothesis)
        except HypothesisError as exc:
            errors.append(str(exc))
    evidence_ids = {item.id for item in evidence}
    if len(evidence_ids) != len(evidence):
        errors.append("Evidence ids must be unique")

    for item in evidence:
        try:
            board.link(item)
        except HypothesisError as exc:
            errors.append(f"Evidence {item.id}: {exc}")
    for hypothesis in board.hypotheses:
        for evidence_id in hypothesis.supporting_evidence + hypothesis.contradicting_evidence:
            if evidence_id not in evidence_ids:
                errors.append(f"Hypothesis {hypothesis.id} cites unknown evidence {evidence_id!r}")
        both = set(hypothesis.supporting_evidence) & set(hypothesis.contradicting_evidence)
        if both:
            errors.append(f"Hypothesis {hypothesis.id}: evidence {sorted(both)} both supports and contradicts it")
        problem = status_problem(hypothesis)
        if problem:
            errors.append(problem)
    return board.hypotheses, errors


def _append(values: list[str], value: str) -> None:
    if value not in values:
        values.append(value)
