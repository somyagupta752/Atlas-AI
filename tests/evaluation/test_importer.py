"""Historical import: originals untouched, nothing invented, totals validated."""

import hashlib
import json
import shutil

import pytest

from app.evaluation.importer import (
    HISTORICAL_VERSION,
    RESULTS_DIR,
    ImportRejectedError,
    import_result_file,
)
from app.evaluation.repository import RunRepository

OLD = RESULTS_DIR / "2026-11-08-live-opus-5-5.json"  # before attempts, categories or held-out were recorded
NEW = RESULTS_DIR / "2026-11-08-live-recorded-opus-5-5.json"


@pytest.fixture
def old_copy(tmp_path):
    """The real file plus a nonce, so its hash is new even after the real file was imported for real."""
    return unique_copy(OLD, tmp_path)


@pytest.fixture
def new_copy(tmp_path):
    return unique_copy(NEW, tmp_path)


def unique_copy(source, tmp_path):
    import uuid

    data = json.loads(source.read_text())
    data["test_nonce"] = str(uuid.uuid4())
    target = tmp_path / source.name
    target.write_text(json.dumps(data))
    return target


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_old_format_imports_without_inventing_missing_fields(db_session, old_copy):
    before = sha(old_copy)
    run, created = import_result_file(db_session, old_copy)
    assert created and sha(old_copy) == before
    assert run.is_historical and run.source_filename == OLD.name and run.source_sha256 == before
    assert run.benchmark_version == HISTORICAL_VERSION and run.imported_at and run.configuration_id is None
    summary = json.loads(OLD.read_text())["summary"]
    cases = RunRepository(db_session).list_cases(run.id, limit=100, offset=0)[0]
    assert len(cases) == summary["cases"] == run.total_cases
    for c in cases:
        assert c.category is None  # never recorded
        assert c.heldout_passed is None and c.exploratory_passed is None and c.diagnosis_correct is None
        assert c.final_verified is True and c.success is True  # recorded outcomes kept as they were
        assert c.attempts and c.attempts[0].unified_diff
    assert float(run.total_cost_usd) == pytest.approx(summary["total_cost_usd"], abs=1e-5)
    metrics = {m.metric_name: m for m in RunRepository(db_session).latest_calculation(run.id).metrics
               if m.grouping_key is None}
    assert metrics["heldout_pass_rate"].metric_value is None  # never recorded: null, not 0%
    assert metrics["final_verified_rate"].metric_value == summary["verified_patch_rate"]


def test_new_format_keeps_heldout_and_diagnosis_fields(db_session, new_copy):
    run, _ = import_result_file(db_session, new_copy)
    summary = json.loads(NEW.read_text())["summary"]
    cases = RunRepository(db_session).list_cases(run.id, limit=100, offset=0)[0]
    assert all(c.category for c in cases)
    assert sum(c.heldout_passed is not None for c in cases) == summary["holdout_cases"]


def test_importing_the_same_file_twice_is_a_no_op(db_session, old_copy):
    first, created = import_result_file(db_session, old_copy)
    second, created_again = import_result_file(db_session, old_copy)
    assert created and not created_again and first.id == second.id


def test_inconsistent_totals_abort_the_import(db_session, tmp_path):
    data = json.loads(OLD.read_text())
    data["test_nonce"] = "x"
    data["summary"]["total_cost_usd"] = 99.0
    broken = tmp_path / "broken.json"
    broken.write_text(json.dumps(data))
    with pytest.raises(ImportRejectedError, match="total_cost_usd"):
        import_result_file(db_session, broken)


def test_non_result_files_are_rejected(db_session):
    with pytest.raises(ImportRejectedError):
        import_result_file(db_session, RESULTS_DIR / "baseline-2026-11-08.manifest.json")


def test_copy_of_a_file_with_another_name_is_still_detected(db_session, tmp_path, old_copy):
    copy = tmp_path / "renamed.json"
    shutil.copy(old_copy, copy)
    first, _ = import_result_file(db_session, old_copy)
    again, created = import_result_file(db_session, copy)
    assert not created and again.id == first.id


def test_the_real_files_are_never_modified_by_importing():
    names = sorted(p.name for p in RESULTS_DIR.glob("*.json"))
    assert "2026-11-08-live-opus-5-5.json" in names
