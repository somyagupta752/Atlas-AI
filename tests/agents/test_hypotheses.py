import pytest

from app.agents.hypotheses import HypothesisBoard, HypothesisError, reconcile
from app.agents.schemas import Evidence, Hypothesis


def ev(evidence_id, supports=(), contradicts=()):
    return Evidence(id=evidence_id, source_type="source_code", observation_ids=["obs-1"], observation="fact",
                    supports_hypothesis_ids=list(supports), contradicts_hypothesis_ids=list(contradicts))


def test_lifecycle_proposed_supported_rejected_inconclusive():
    board = HypothesisBoard()
    board.propose("H1", "Repository returns None")
    board.propose("H2", "Controller handles None incorrectly")
    board.propose("H3", "Database connection is failing")
    assert {h.status for h in board.hypotheses} == {"proposed"}

    board.link(ev("E1", supports=["H1"]))
    board.link(ev("E2", supports=["H2"], contradicts=["H3"]))
    board.set_status("H1", "supported")
    board.set_status("H2", "supported")
    board.set_status("H3", "rejected")
    assert [h.id for h in board.supported()] == ["H1", "H2"]
    assert board.set_status("H3", "inconclusive").status == "inconclusive"


def test_status_must_match_evidence():
    board = HypothesisBoard()
    board.propose("H1", "x")
    with pytest.raises(HypothesisError, match="no supporting"):
        board.set_status("H1", "supported")
    with pytest.raises(HypothesisError, match="no contradicting"):
        board.set_status("H1", "rejected")


def test_new_evidence_can_reject_a_hypothesis():
    board = HypothesisBoard()
    board.propose("H1", "x")
    board.link(ev("E1", supports=["H1"]))
    board.set_status("H1", "supported")
    board.link(ev("E2", contradicts=["H1"]))
    assert board.set_status("H1", "rejected").contradicting_evidence == ["E2"]


def test_reconcile_merges_links_and_reports_problems():
    hypotheses = [
        Hypothesis(id="H1", description="a", status="supported", supporting_evidence=["E1"]),
        Hypothesis(id="H2", description="b", status="rejected"),
        Hypothesis(id="H3", description="c", supporting_evidence=["E9"]),
    ]
    merged, errors = reconcile(hypotheses, [ev("E1", supports=["H1"]), ev("E2", contradicts=["H2"]), ev("E3", supports=["H7"])])

    by_id = {h.id: h for h in merged}
    assert by_id["H1"].supporting_evidence == ["E1"]  # no duplicate
    assert by_id["H2"].contradicting_evidence == ["E2"]  # linked from the evidence side
    assert any("E9" in e for e in errors)
    assert any("H7" in e for e in errors)


def test_duplicate_ids():
    _, errors = reconcile([Hypothesis(id="H1", description="a"), Hypothesis(id="H1", description="b")], [])
    assert any("Duplicate" in e for e in errors)
