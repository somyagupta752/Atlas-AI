"""create evaluation laboratory tables

Revision ID: b3f1a8c2d9e4
Revises: 9e4b7c2d1a3f
Create Date: 2026-11-08 18:00:00

Raw case and attempt rows are evidence. Foreign keys are RESTRICT, never CASCADE,
so no delete silently erases history. Triggers add the guarantees a foreign key
cannot give:

* ``evaluation_configurations``: rows are never updated or deleted. A change is a new version.
* ``evaluation_runs``: a run in a terminal status cannot be updated or deleted.
* ``evaluation_case_results`` / ``evaluation_attempts``: cannot be inserted, updated or deleted once
  the run is terminal.
* metric calculations and snapshots: append-only.

Recalculating metrics adds a calculation; it does not touch the evidence.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "b3f1a8c2d9e4"
down_revision: str | Sequence[str] | None = "9e4b7c2d1a3f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSONB = postgresql.JSONB(astext_type=sa.Text())
MONEY = sa.Numeric(12, 6)
TERMINAL = "('completed', 'partial', 'failed', 'cancelled')"


def _in(column: str, values: Sequence[str]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _created() -> sa.Column:
    return sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False)


def upgrade() -> None:
    op.create_table(
        "evaluation_configurations",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("parent_configuration_id", sa.UUID(),
                  sa.ForeignKey("evaluation_configurations.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("config", JSONB, nullable=False),
        sa.Column("config_hash", sa.String(64), nullable=False),
        sa.Column("model_provider", sa.String(50), nullable=False),
        sa.Column("model_id", sa.String(200), nullable=False),
        sa.Column("retrieval_strategy", sa.String(50), nullable=False),
        sa.Column("test_visibility", sa.String(50), nullable=False),
        sa.Column("test_backend", sa.String(50), nullable=False),
        _created(),
        sa.UniqueConstraint("name", "version", name="uq_evaluation_configurations_name_version"),
        sa.CheckConstraint("version >= 1", name="ck_evaluation_configurations_version"),
    )
    op.create_index("ix_evaluation_configurations_config_hash", "evaluation_configurations", ["config_hash"])
    op.create_index("ix_evaluation_configurations_created_at", "evaluation_configurations", ["created_at"])
    op.create_index("ix_evaluation_configurations_model", "evaluation_configurations",
                    ["model_provider", "model_id"])

    op.create_table(
        "evaluation_runs",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("evaluation_mode", sa.String(20), nullable=False),
        sa.Column("diagnosis_mode", sa.String(20), nullable=False),
        sa.Column("configuration_id", sa.UUID(),
                  sa.ForeignKey("evaluation_configurations.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("configuration_snapshot", JSONB, nullable=False),
        sa.Column("benchmark_version", sa.String(100), nullable=False),
        sa.Column("selected_case_ids", JSONB, nullable=False),
        sa.Column("total_cases", sa.Integer(), nullable=False),
        sa.Column("environment", JSONB, nullable=False),
        sa.Column("budget_limit_usd", MONEY, nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("total_cost_usd", MONEY, nullable=True),
        sa.Column("cost_kind", sa.String(10), nullable=True),
        sa.Column("total_duration_seconds", sa.Float(), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("idempotency_key", sa.String(100), nullable=True, unique=True),
        sa.Column("is_historical", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("source_filename", sa.String(300), nullable=True),
        sa.Column("source_sha256", sa.String(64), nullable=True, unique=True),
        sa.Column("imported_at", sa.DateTime(timezone=True), nullable=True),
        _created(),
        sa.CheckConstraint(_in("status", ["pending", "running", "completed", "partial", "failed", "cancelled"]),
                           name="ck_evaluation_runs_status"),
        sa.CheckConstraint(_in("evaluation_mode", ["reference", "live"]), name="ck_evaluation_runs_mode"),
        sa.CheckConstraint(_in("diagnosis_mode", ["agent", "recorded"]), name="ck_evaluation_runs_diagnosis_mode"),
        sa.CheckConstraint("total_cases >= 0", name="ck_evaluation_runs_total_cases"),
        sa.CheckConstraint("cost_kind IS NULL OR " + _in("cost_kind", ["actual", "estimated"]),
                           name="ck_evaluation_runs_cost_kind"),
    )
    op.create_index("ix_evaluation_runs_created_at", "evaluation_runs", ["created_at"])
    op.create_index("ix_evaluation_runs_status", "evaluation_runs", ["status"])
    op.create_index("ix_evaluation_runs_configuration_id", "evaluation_runs", ["configuration_id"])
    op.create_index("ix_evaluation_runs_benchmark_version", "evaluation_runs", ["benchmark_version"])

    op.create_table(
        "evaluation_case_results",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("evaluation_run_id", sa.UUID(), sa.ForeignKey("evaluation_runs.id", ondelete="RESTRICT"),
                  nullable=False),
        sa.Column("case_id", sa.String(100), nullable=False),
        sa.Column("variant", sa.String(100), server_default="live", nullable=False),
        sa.Column("category", sa.String(100), nullable=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("execution_status", sa.String(30), nullable=True),
        sa.Column("diagnosis_status", sa.String(30), nullable=True),
        sa.Column("diagnosis_correct", sa.Boolean(), nullable=True),
        sa.Column("diagnosis_score", sa.Float(), nullable=True),
        sa.Column("patch_generated", sa.Boolean(), nullable=True),
        sa.Column("patch_valid", sa.Boolean(), nullable=True),
        sa.Column("patch_applied", sa.Boolean(), nullable=True),
        sa.Column("first_attempt_verified", sa.Boolean(), nullable=True),
        sa.Column("final_verified", sa.Boolean(), nullable=True),
        sa.Column("repaired_by_retry", sa.Boolean(), nullable=True),
        sa.Column("success", sa.Boolean(), nullable=True),
        sa.Column("heldout_passed", sa.Boolean(), nullable=True),
        sa.Column("exploratory_passed", sa.Boolean(), nullable=True),
        sa.Column("regression_detected", sa.Boolean(), nullable=True),
        sa.Column("attempts_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("total_cost_usd", MONEY, nullable=True),
        sa.Column("cost_kind", sa.String(10), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("total_latency_seconds", sa.Float(), nullable=True),
        sa.Column("diagnosis_record_id", sa.UUID(), sa.ForeignKey("investigations.id", ondelete="RESTRICT"),
                  nullable=True),
        sa.Column("patch_execution_id", sa.UUID(), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("evidence", JSONB, nullable=False),
        _created(),
        sa.UniqueConstraint("evaluation_run_id", "case_id", "variant", name="uq_evaluation_cases_run_case_variant"),
        sa.CheckConstraint(_in("status", ["pending", "running", "completed", "failed", "incomplete"]),
                           name="ck_evaluation_cases_status"),
        sa.CheckConstraint("attempts_count >= 0", name="ck_evaluation_cases_attempts"),
        sa.CheckConstraint("cost_kind IS NULL OR " + _in("cost_kind", ["actual", "estimated"]),
                           name="ck_evaluation_cases_cost_kind"),
    )
    op.create_index("ix_evaluation_cases_run", "evaluation_case_results", ["evaluation_run_id"])
    op.create_index("ix_evaluation_cases_case_id", "evaluation_case_results", ["case_id"])
    op.create_index("ix_evaluation_cases_status", "evaluation_case_results", ["status"])
    op.create_index("ix_evaluation_cases_created_at", "evaluation_case_results", ["created_at"])
    op.create_index("ix_evaluation_cases_category", "evaluation_case_results", ["category"])

    op.create_table(
        "evaluation_attempts",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("evaluation_case_result_id", sa.UUID(),
                  sa.ForeignKey("evaluation_case_results.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("patch_valid", sa.Boolean(), nullable=True),
        sa.Column("patch_applied", sa.Boolean(), nullable=True),
        sa.Column("required_tests_passed", sa.Boolean(), nullable=True),
        sa.Column("heldout_tests_passed", sa.Boolean(), nullable=True),
        sa.Column("regression_detected", sa.Boolean(), nullable=True),
        sa.Column("feedback_from_attempts", JSONB, nullable=False),
        sa.Column("feedback_reasons", JSONB, nullable=False),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("unified_diff", sa.Text(), nullable=True),
        sa.Column("diff_sha256", sa.String(64), nullable=True),
        sa.Column("patch_proposal_id", sa.UUID(), nullable=True),
        sa.Column("cost_usd", MONEY, nullable=True),
        sa.Column("cost_kind", sa.String(10), nullable=True),
        sa.Column("latency_seconds", sa.Float(), nullable=True),
        sa.Column("evidence", JSONB, nullable=False),
        _created(),
        sa.UniqueConstraint("evaluation_case_result_id", "attempt_number", name="uq_evaluation_attempts_case_number"),
        sa.CheckConstraint("attempt_number >= 1", name="ck_evaluation_attempts_number"),
        sa.CheckConstraint(
            _in("status", ["verified", "rejected", "inconclusive", "failed", "validation_failed", "incomplete"]),
            name="ck_evaluation_attempts_status"),
        sa.CheckConstraint("cost_kind IS NULL OR " + _in("cost_kind", ["actual", "estimated"]),
                           name="ck_evaluation_attempts_cost_kind"),
    )
    op.create_index("ix_evaluation_attempts_case", "evaluation_attempts", ["evaluation_case_result_id"])
    op.create_index("ix_evaluation_attempts_created_at", "evaluation_attempts", ["created_at"])
    op.create_index("ix_evaluation_attempts_status", "evaluation_attempts", ["status"])

    op.create_table(
        "evaluation_metric_calculations",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("evaluation_run_id", sa.UUID(), sa.ForeignKey("evaluation_runs.id", ondelete="RESTRICT"),
                  nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(300), nullable=False),
        sa.Column("metrics_version", sa.String(50), nullable=False),
        sa.Column("evidence_sha256", sa.String(64), nullable=False),
        sa.Column("calculated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("evaluation_run_id", "version", name="uq_evaluation_calculations_run_version"),
        sa.CheckConstraint("version >= 1", name="ck_evaluation_calculations_version"),
    )
    op.create_index("ix_evaluation_metric_calculations_evaluation_run_id", "evaluation_metric_calculations",
                    ["evaluation_run_id"])

    op.create_table(
        "evaluation_metric_snapshots",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("evaluation_run_id", sa.UUID(), sa.ForeignKey("evaluation_runs.id", ondelete="RESTRICT"),
                  nullable=False),
        sa.Column("calculation_id", sa.UUID(),
                  sa.ForeignKey("evaluation_metric_calculations.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("metric_name", sa.String(100), nullable=False),
        sa.Column("metric_value", sa.Float(), nullable=True),
        sa.Column("numerator", sa.Float(), nullable=False),
        sa.Column("denominator", sa.Float(), nullable=False),
        sa.Column("grouping_key", sa.String(200), nullable=True),
        sa.Column("calculated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("calculation_id", "metric_name", "grouping_key",
                            name="uq_evaluation_metrics_calculation_name_group", postgresql_nulls_not_distinct=True),
        sa.CheckConstraint("denominator >= 0 AND numerator >= 0", name="ck_evaluation_metrics_counts"),
    )
    op.create_index("ix_evaluation_metrics_run", "evaluation_metric_snapshots", ["evaluation_run_id"])
    op.create_index("ix_evaluation_metrics_name", "evaluation_metric_snapshots", ["metric_name"])

    _create_immutability_triggers()


def _create_immutability_triggers() -> None:
    op.execute(
        """
        CREATE FUNCTION evaluation_reject_change() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION '% rows are immutable: % is not allowed', TG_TABLE_NAME, TG_OP
                USING ERRCODE = 'integrity_constraint_violation';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE FUNCTION evaluation_guard_run() RETURNS trigger AS $$
        BEGIN
            IF OLD.status IN ('completed', 'partial', 'failed', 'cancelled') THEN
                RAISE EXCEPTION 'evaluation run % is %; finished runs are immutable', OLD.id, OLD.status
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF TG_OP = 'DELETE' THEN
                RETURN OLD;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE FUNCTION evaluation_guard_children() RETURNS trigger AS $$
        DECLARE
            run_id uuid;
            run_status text;
        BEGIN
            IF TG_TABLE_NAME = 'evaluation_case_results' THEN
                run_id := COALESCE(NEW.evaluation_run_id, OLD.evaluation_run_id);
            ELSE
                SELECT c.evaluation_run_id INTO run_id FROM evaluation_case_results c
                WHERE c.id = COALESCE(NEW.evaluation_case_result_id, OLD.evaluation_case_result_id);
            END IF;
            SELECT status INTO run_status FROM evaluation_runs WHERE id = run_id;
            IF run_status IN ('completed', 'partial', 'failed', 'cancelled') THEN
                RAISE EXCEPTION '% of finished evaluation run % cannot be changed (%)', TG_TABLE_NAME, run_id, TG_OP
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF TG_OP = 'DELETE' THEN
                RETURN OLD;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute("""CREATE TRIGGER evaluation_configurations_immutable BEFORE UPDATE OR DELETE
                  ON evaluation_configurations FOR EACH ROW EXECUTE FUNCTION evaluation_reject_change();""")
    op.execute("""CREATE TRIGGER evaluation_runs_guard BEFORE UPDATE OR DELETE
                  ON evaluation_runs FOR EACH ROW EXECUTE FUNCTION evaluation_guard_run();""")
    for table in ("evaluation_case_results", "evaluation_attempts"):
        op.execute(f"""CREATE TRIGGER {table}_guard BEFORE INSERT OR UPDATE OR DELETE
                       ON {table} FOR EACH ROW EXECUTE FUNCTION evaluation_guard_children();""")
    for table in ("evaluation_metric_calculations", "evaluation_metric_snapshots"):
        op.execute(f"""CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE
                       ON {table} FOR EACH ROW EXECUTE FUNCTION evaluation_reject_change();""")


def downgrade() -> None:
    for table, trigger in (
        ("evaluation_metric_snapshots", "evaluation_metric_snapshots_append_only"),
        ("evaluation_metric_calculations", "evaluation_metric_calculations_append_only"),
        ("evaluation_attempts", "evaluation_attempts_guard"),
        ("evaluation_case_results", "evaluation_case_results_guard"),
        ("evaluation_runs", "evaluation_runs_guard"),
        ("evaluation_configurations", "evaluation_configurations_immutable"),
    ):
        op.execute(f"DROP TRIGGER IF EXISTS {trigger} ON {table}")
    op.execute("DROP FUNCTION IF EXISTS evaluation_guard_children()")
    op.execute("DROP FUNCTION IF EXISTS evaluation_guard_run()")
    op.execute("DROP FUNCTION IF EXISTS evaluation_reject_change()")
    op.drop_table("evaluation_metric_snapshots")
    op.drop_table("evaluation_metric_calculations")
    op.drop_table("evaluation_attempts")
    op.drop_table("evaluation_case_results")
    op.drop_table("evaluation_runs")
    op.drop_table("evaluation_configurations")
