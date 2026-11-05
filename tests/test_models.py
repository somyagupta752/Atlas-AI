import uuid

from app.models import BenchmarkExecution


def test_table_schema():
    table = BenchmarkExecution.__table__

    assert table.name == "benchmark_executions"
    assert set(table.columns.keys()) == {
        "id",
        "case_id",
        "outcome",
        "exit_code",
        "stdout",
        "stderr",
        "duration_seconds",
        "timed_out",
        "created_at",
    }
    assert table.columns["id"].primary_key
    assert table.columns["exit_code"].nullable is True
    assert table.columns["case_id"].nullable is False
    assert table.columns["created_at"].server_default is not None


def test_can_build_instance_without_database():
    execution = BenchmarkExecution(
        id=uuid.uuid4(),
        case_id="bug-001-average",
        outcome="reproduced",
        exit_code=1,
        stdout="out",
        stderr="",
        duration_seconds=0.5,
        timed_out=False,
    )

    assert execution.case_id == "bug-001-average"
    assert execution.exit_code == 1
