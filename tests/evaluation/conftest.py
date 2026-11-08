"""Builders for evaluation tests. Real PostgreSQL through the rollback-wrapped ``db_session``."""

import uuid
from decimal import Decimal

import pytest

from app.evaluation.models import (
    EvaluationAttempt,
    EvaluationCaseResult,
    EvaluationConfiguration,
    EvaluationRun,
)


@pytest.fixture
def make_config(db_session):
    def make(name=None, version=1, **overrides):
        values = {
            "name": name or f"cfg-{uuid.uuid4()}", "version": version, "config": {"model": {"model_id": "m"}},
            "config_hash": uuid.uuid4().hex + uuid.uuid4().hex[:32], "model_provider": "anthropic",
            "model_id": "claude-opus-5-5", "retrieval_strategy": "hybrid", "test_visibility": "blind",
            "test_backend": "container",
        }
        values.update(overrides)
        config = EvaluationConfiguration(**values)
        db_session.add(config)
        db_session.flush()
        return config

    return make


@pytest.fixture
def make_run(db_session):
    def make(config=None, status="running", **overrides):
        values = {
            "name": "run", "status": status, "evaluation_mode": "reference", "diagnosis_mode": "recorded",
            "configuration_id": config.id if config else None,
            "configuration_snapshot": config.config if config else {}, "benchmark_version": "bench-1",
            "selected_case_ids": ["bug-001"], "total_cases": 1, "environment": {},
        }
        values.update(overrides)
        run = EvaluationRun(**values)
        db_session.add(run)
        db_session.flush()
        return run

    return make


@pytest.fixture
def make_case(db_session):
    def make(run, case_id="bug-001", **overrides):
        values = {"evaluation_run_id": run.id, "case_id": case_id, "status": "completed", "evidence": {}}
        values.update(overrides)
        case = EvaluationCaseResult(**values)
        db_session.add(case)
        db_session.flush()
        return case

    return make


@pytest.fixture
def make_attempt(db_session):
    def make(case, number=1, **overrides):
        values = {
            "evaluation_case_result_id": case.id, "attempt_number": number, "status": "verified",
            "feedback_from_attempts": [], "feedback_reasons": [], "evidence": {},
        }
        values.update(overrides)
        attempt = EvaluationAttempt(**values)
        db_session.add(attempt)
        db_session.flush()
        return attempt

    return make


def money(value: str) -> Decimal:
    return Decimal(value)
