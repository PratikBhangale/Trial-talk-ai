"""Nightly telemetry retention in the reconcile Lambda."""
import json

import pytest

from conftest import finished, needs_sdk


class _Scheduler:
    def get_paginator(self, name):
        return type("P", (), {"paginate": lambda self, **kw: [{"Schedules": []}]})()


@pytest.fixture
def rec(aws, reconcile, monkeypatch):
    monkeypatch.setattr(reconcile, "scheduler", _Scheduler())
    monkeypatch.delenv("DRY_RUN", raising=False)
    monkeypatch.delenv("TELEMETRY_RETENTION_DAYS", raising=False)

    def respond(sql, params):
        if "telemetry_spans" in sql:
            return [{"n": 7}]
        return []
    aws.rds.respond = respond
    return reconcile


def prune_statements(aws):
    return [(sql, p) for sql, p in aws.rds.statements if "telemetry_spans" in sql]


def test_prune_deletes_and_reports_count(aws, rec, monkeypatch, capsys):
    monkeypatch.setenv("DRY_RUN", "0")
    result = rec.handler({}, None)
    assert result["pruned"] == 7
    ((sql, params),) = prune_statements(aws)
    assert "DELETE FROM trial.telemetry_spans" in sql and "start_ts" in sql
    assert params == {"d": "30"}
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])["pruned"] == 7


def test_retention_days_from_env(aws, rec, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "0")
    monkeypatch.setenv("TELEMETRY_RETENTION_DAYS", "14")
    rec.handler({}, None)
    assert prune_statements(aws)[0][1] == {"d": "14"}


def test_dry_run_counts_but_never_deletes(aws, rec, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "1")
    result = rec.handler({}, None)
    assert result["pruned"] == 7
    stmts = prune_statements(aws)
    assert len(stmts) == 1 and "DELETE" not in stmts[0][0].upper()


def test_dry_run_event_override_still_dry(aws, rec, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "0")
    rec.handler({"dry_run": True}, None)
    assert all("DELETE" not in sql.upper() for sql, _ in prune_statements(aws))


@pytest.mark.parametrize("days", ["0", "-5"])
def test_retention_zero_skips_entirely(aws, rec, monkeypatch, days):
    monkeypatch.setenv("DRY_RUN", "0")
    monkeypatch.setenv("TELEMETRY_RETENTION_DAYS", days)
    result = rec.handler({}, None)
    assert result["pruned"] is None
    assert prune_statements(aws) == []


def test_prune_exception_does_not_break_reconcile(aws, rec, monkeypatch, capsys):
    monkeypatch.setenv("DRY_RUN", "0")

    def respond(sql, params):
        if "telemetry_spans" in sql:
            raise RuntimeError("permission denied for table telemetry_spans")
        return []
    aws.rds.respond = respond
    result = rec.handler({}, None)
    assert result["pruned"] is None and result["dry_run"] is False
    assert result["created"] == [] and result["deleted"] == []
    assert "prune failed" in capsys.readouterr().out


def test_bad_retention_value_falls_back_to_30(aws, rec, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "0")
    monkeypatch.setenv("TELEMETRY_RETENTION_DAYS", "soon")
    assert rec.handler({}, None)["pruned"] == 7
    assert prune_statements(aws)[0][1] == {"d": "30"}


@needs_sdk
def test_prune_span_is_child_of_run(aws, rec, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "0")
    rec.handler({}, None)
    (run,) = finished(aws, "reconcile.run")
    (prune,) = finished(aws, "reconcile.prune")
    assert prune.parent.span_id == run.context.span_id
    assert prune.attributes["trialtalk.pruned"] == 7
