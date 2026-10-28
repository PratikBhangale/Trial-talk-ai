"""Staff clinic view: pure helpers and the /staff/ask hookup (no DB), then the two clinic endpoints end to end
(needs TT_PG_BIN; see conftest)."""

import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from conftest import OTHER_SITE_CLAIMS, STAFF_CLAIMS, _event

TRIAL_KEYS = {"study_id", "name", "phase", "condition", "patients", "engaged_pct", "adherence_pct", "symptoms",
              "critical", "agent_handled"}
SUMMARY_KEYS = {"trials", "patients", "pct_not_engaging", "critical_open"}

# ------------------------------------------------------------------ pure helpers


def _row(**kw):
    base = {"study_id": "S-1", "name": "Study", "phase": None, "condition": None, "description": None, "patients": 0,
            "engaged": 0, "taken": 0, "due": 0, "symptoms": 0, "urgent": 0, "critical": 0, "critical_open": 0}
    return base | kw


def test_trial_row_math_and_shape():
    from staff_api import clinic
    row = clinic.trial_row(_row(patients=3, engaged=1, taken=3, due=5, symptoms=4, urgent=1, critical=2))
    assert set(row) == TRIAL_KEYS
    assert row["engaged_pct"] == 33.3 and row["adherence_pct"] == 60.0
    assert row["symptoms"] == 4 and row["agent_handled"] == 3 and row["critical"] == 2


def test_trial_row_zero_state_is_zeros_not_errors():
    from staff_api import clinic
    row = clinic.trial_row(_row())
    assert row["patients"] == 0 and row["engaged_pct"] == 0.0 and row["adherence_pct"] is None
    assert row["symptoms"] == 0 and row["agent_handled"] == 0 and row["critical"] == 0
    assert clinic.summarize([]) == {"trials": 0, "patients": 0, "pct_not_engaging": 0.0, "critical_open": 0}


def test_summary_pools_patients_across_trials():
    from staff_api import clinic
    s = clinic.summarize([_row(patients=3, engaged=1, critical_open=1), _row(study_id="S-2", patients=1, engaged=1)])
    assert s == {"trials": 2, "patients": 4, "pct_not_engaging": 50.0, "critical_open": 1}


def test_series_is_14_days_zero_filled_oldest_first():
    from staff_api import clinic
    today = date(2026, 10, 28)
    out = clinic.series([{"day": "2026-10-28", "total": 2, "critical": 1}, {"day": date(2026, 10, 15), "total": 5,
                                                                          "critical": 0},
                         {"day": "2026-10-14", "total": 9, "critical": 9}], today)   # the 15th is the oldest day kept
    assert len(out) == 14
    assert out[0] == {"day": "2026-10-15", "total": 5, "critical": 0}
    assert out[-1] == {"day": "2026-10-28", "total": 2, "critical": 1}
    assert all(set(d) == {"day", "total", "critical"} for d in out)
    assert [d["total"] for d in out[1:-1]] == [0] * 12


# ------------------------------------------------------------------ /staff/ask hookup (no DB)

def _post_ask(claims=STAFF_CLAIMS, body=None):
    """POST /staff/ask through the real router (below the telemetry wrapper, which needs the OTel SDK for 5xx)."""
    import staff_api.handler as staff
    resp = staff.router._dispatch(_event("POST", "/staff/ask", claims, body), staff.staff_claims, "POST", "/staff/ask")
    return resp["statusCode"], json.loads(resp["body"])


def _ask(monkeypatch, module):
    """POST /staff/ask with staff_api.ask replaced by `module` (None = absent)."""
    monkeypatch.setitem(sys.modules, "staff_api.ask", module)
    if module is None:
        monkeypatch.delattr(sys.modules["staff_api"], "ask", raising=False)
    return _post_ask(body={"question": "how many?"})


def test_ask_absent_module_is_a_clean_404(monkeypatch):
    status, body = _ask(monkeypatch, None)
    assert status == 404 and body == {"error": "Ask is not available yet"}


def test_ask_routes_to_ask_handler_with_event_staff_body(monkeypatch):
    import types
    seen = {}

    def ask_handler(event, staff, body):
        seen.update(event=event, staff=staff, body=body)
        return 200, {"answer": "ok"}

    status, body = _ask(monkeypatch, types.SimpleNamespace(ask_handler=ask_handler))
    assert (status, body) == (200, {"answer": "ok"})
    assert seen["body"] == {"question": "how many?"} and seen["staff"].site_ids == ["RIVERSIDE"]
    assert seen["event"]["rawPath"] == "/staff/ask"


def test_ask_real_import_errors_are_not_masked(monkeypatch):
    import importlib.abc
    import importlib.util
    import staff_api.handler as staff

    # ask.py exists but one of its own imports is missing: that is a bug (500), not "not available yet"
    class Broken(importlib.abc.MetaPathFinder, importlib.abc.Loader):
        def find_spec(self, name, path, target=None):
            return importlib.util.spec_from_loader(name, self) if name == "staff_api.ask" else None

        def create_module(self, spec):
            return None

        def exec_module(self, module):
            raise ModuleNotFoundError("No module named 'nope'", name="nope")

    monkeypatch.delitem(sys.modules, "staff_api.ask", raising=False)
    monkeypatch.setattr(sys, "meta_path", [Broken(), *sys.meta_path])
    assert _post_ask(body={})[0] == 500


def test_ask_requires_staff():
    assert _post_ask(claims={"email": "x@y.z"})[0] == 403


# ------------------------------------------------------------------ endpoints

def _ago(**kw):
    return datetime.now(timezone.utc) - timedelta(**kw)


@pytest.fixture
def studies(admin):
    """trial.studies from db/07_studies.sql when the local bootstrap has not applied it yet."""
    if admin.execute("SELECT to_regclass('trial.studies')").fetchone()[0] is None:
        from local_env import DB_SQL, run_sql_file
        run_sql_file(admin, Path(DB_SQL) / "07_studies.sql")
    yield


def _participant(admin, pid, study, site="RIVERSIDE", status="enrolled"):
    admin.execute("INSERT INTO pii.participants (participant_id, full_name, phone_e164, study_id, site_id, status) "
                  "VALUES (%s, %s, %s, %s, %s, %s)", (pid, f"Name {pid}", f"+1555000{pid[-4:]}", study, site, status))


def _inbound(admin, pid, ago):
    admin.execute("INSERT INTO trial.communications (participant_id, channel, direction, msg_type, body_text, "
                  "created_at) VALUES (%s, 'whatsapp', 'inbound', 'text', 'hi', %s)", (pid, _ago(**ago)))


def _symptom(admin, pid, ago, status="logged", hint="mild", supersedes=None):
    row = admin.execute(
        "INSERT INTO trial.clinical_events (participant_id, record_type, status, source, verbatim_text, "
        "severity_hint, occurred_at, supersedes_event_id, change_reason, created_by) "
        "VALUES (%s, 'SYMPTOM', %s, 'whatsapp', 'my knee hurts', %s, %s, %s, %s, 'agent') RETURNING event_id",
        (pid, status, hint, _ago(**ago), supersedes, "correction" if supersedes else None)).fetchone()
    return row[0]


def _dose(admin, pid, ago, response):
    admin.execute("INSERT INTO trial.clinical_events (participant_id, record_type, source, dose_label, scheduled_at, "
                  "response, created_by) VALUES (%s, 'DOSE', 'scheduler', 'evening', %s, %s, 'reminder-lambda')",
                  (pid, _ago(**ago), response))


def _alert(admin, pid, ago, status="open"):
    admin.execute("INSERT INTO trial.emergency_alerts (participant_id, source, status, created_at) "
                  "VALUES (%s, 'sos_button', %s, %s)", (pid, status, _ago(**ago)))


@pytest.fixture
def clinic_data(admin, studies):
    # RIVERSIDE, KOA-301: 3 active patients (one withdrawn, excluded), only P-9001 engaged
    _participant(admin, "P-9001", "KOA-301")
    _participant(admin, "P-9002", "KOA-301")
    _participant(admin, "P-9004", "KOA-301")
    _participant(admin, "P-9003", "KOA-301", status="withdrawn")
    _inbound(admin, "P-9001", {"days": 2})
    _inbound(admin, "P-9002", {"days": 20})                      # too old to count
    _inbound(admin, "P-9003", {"days": 1})                       # withdrawn: not counted at all
    admin.execute("INSERT INTO trial.communications (participant_id, channel, direction, msg_type, body_text) "
                  "VALUES ('P-9004', 'whatsapp', 'outbound', 'text', 'reminder')")   # outbound is not engagement
    # doses: 3 taken, 1 skipped, 1 no_response in the window = 60%; old and future ones do not count
    for hours in (24, 48, 72):
        _dose(admin, "P-9001", {"hours": hours}, "taken")
    _dose(admin, "P-9001", {"hours": 96}, "skipped")
    _dose(admin, "P-9001", {"hours": 120}, "no_response")
    _dose(admin, "P-9001", {"days": 20}, "taken")
    _dose(admin, "P-9001", {"hours": -5}, "no_response")
    # symptoms: 4 current (one correction replaces its original), 1 of them urgent
    _symptom(admin, "P-9001", {"hours": 1})
    _symptom(admin, "P-9001", {"days": 2})
    _symptom(admin, "P-9001", {"hours": 2}, status="escalated", hint="urgent")
    first = _symptom(admin, "P-9002", {"days": 3})
    _symptom(admin, "P-9002", {"days": 3}, supersedes=first)
    _symptom(admin, "P-9002", {"days": 40})                       # outside the 14-day series, still counted in total
    _alert(admin, "P-9001", {"hours": 3})
    _alert(admin, "P-9002", {"days": 1}, status="resolved")
    # RIVERSIDE, a study with no trial.studies row
    _participant(admin, "P-9010", "ZZZ-999")
    # LAKESIDE: same study id, other patients, and a study of its own
    _participant(admin, "P-9020", "KOA-301", site="LAKESIDE")
    _participant(admin, "P-9021", "KOA-301", site="LAKESIDE")
    _participant(admin, "P-9022", "LKV-100", site="LAKESIDE")
    _inbound(admin, "P-9020", {"days": 1})
    _inbound(admin, "P-9021", {"days": 1})
    for _ in range(3):
        _symptom(admin, "P-9020", {"hours": 4})
    _alert(admin, "P-9020", {"hours": 1})
    _alert(admin, "P-9021", {"hours": 1})
    _dose(admin, "P-9020", {"hours": 24}, "no_response")


def test_clinic_zero_state(call_staff, studies):
    status, body = call_staff("GET", "/staff/clinic")
    assert status == 200
    assert body == {"summary": {"trials": 0, "patients": 0, "pct_not_engaging": 0.0, "critical_open": 0},
                    "trials": []}
    status, body = call_staff("GET", "/staff/clinic/KOA-301")
    assert status == 404 and "error" in body


def test_clinic_list_shape_and_math(call_staff, admin, clinic_data):
    status, body = call_staff("GET", "/staff/clinic")
    assert status == 200, body
    assert set(body) == {"summary", "trials"} and set(body["summary"]) == SUMMARY_KEYS
    assert all(set(t) == TRIAL_KEYS for t in body["trials"])
    by_id = {t["study_id"]: t for t in body["trials"]}
    assert list(by_id) == ["KOA-301", "ZZZ-999"]                  # LKV-100 is LAKESIDE's; ids sorted
    koa = by_id["KOA-301"]
    assert koa["name"].startswith("KNEE-RELIEF") and koa["phase"] == "Phase 3" and koa["condition"]
    assert koa["patients"] == 3                                   # the withdrawn one and LAKESIDE's are not counted
    assert koa["engaged_pct"] == 33.3                             # only P-9001 wrote in the last 14 days
    assert koa["adherence_pct"] == 60.0                           # 3 taken of 5
    assert koa["symptoms"] == 5 and koa["agent_handled"] == 4 and koa["critical"] == 2
    zzz = by_id["ZZZ-999"]                                        # no trial.studies row: the id stands in as the name
    assert zzz["name"] == "ZZZ-999" and zzz["phase"] is None and zzz["condition"] is None
    assert zzz["patients"] == 1 and zzz["engaged_pct"] == 0.0 and zzz["adherence_pct"] is None
    assert zzz["symptoms"] == 0 and zzz["agent_handled"] == 0 and zzz["critical"] == 0
    assert body["summary"] == {"trials": 2, "patients": 4, "pct_not_engaging": 75.0, "critical_open": 1}


def test_clinic_is_scoped_to_the_callers_sites(call_staff, clinic_data):
    status, body = call_staff("GET", "/staff/clinic", claims=OTHER_SITE_CLAIMS)
    assert status == 200, body
    by_id = {t["study_id"]: t for t in body["trials"]}
    assert list(by_id) == ["KOA-301", "LKV-100"]                  # nothing of RIVERSIDE's, not even ZZZ-999
    koa = by_id["KOA-301"]
    assert koa["patients"] == 2 and koa["engaged_pct"] == 100.0
    assert koa["symptoms"] == 3 and koa["agent_handled"] == 3 and koa["critical"] == 2
    assert koa["adherence_pct"] == 0.0                            # one no_response, no taken
    assert by_id["LKV-100"]["name"] == "LKV-100"
    assert body["summary"] == {"trials": 2, "patients": 3, "pct_not_engaging": 33.3, "critical_open": 2}
    # a study that exists only at the other site is not there for RIVERSIDE
    assert call_staff("GET", "/staff/clinic/LKV-100")[0] == 404
    assert call_staff("GET", "/staff/clinic/ZZZ-999", claims=OTHER_SITE_CLAIMS)[0] == 404


def test_clinic_study_detail(call_staff, clinic_data):
    status, body = call_staff("GET", "/staff/clinic/KOA-301")
    assert status == 200, body
    assert set(body) == {"study", "summary", "symptoms_by_day", "critical_list", "engagement"}
    study = body["study"]
    assert set(study) == {"study_id", "name", "phase", "condition", "description"}
    assert study["study_id"] == "KOA-301" and study["condition"] == "Knee osteoarthritis" and study["description"]
    assert set(body["summary"]) == TRIAL_KEYS
    assert body["summary"]["patients"] == 3 and body["summary"]["agent_handled"] == 4
    assert body["engagement"] == {"engaged": 1, "not_engaging": 2}

    days = body["symptoms_by_day"]
    assert len(days) == 14
    today = datetime.now(timezone.utc).date()
    assert [d["day"] for d in days] == [(today - timedelta(days=13 - i)).isoformat() for i in range(14)]
    by_day = {d["day"]: d for d in days}
    # (symptoms an hour or two old can fall on either side of midnight UTC, so only totals are pinned for them)
    assert sum(d["critical"] for d in days) == 1                  # the one urgent symptom
    assert sum(d["total"] for d in days) == 4                     # current symptoms in the last 14 days; day 40 is out
    assert by_day[(today - timedelta(days=3)).isoformat()]["total"] == 1   # the correction, not its original too

    crit = body["critical_list"]
    assert [c["status"] for c in crit] == ["open", "resolved"]    # newest first
    assert [c["participant_id"] for c in crit] == ["P-9001", "P-9002"]
    assert set(crit[0]) == {"participant_id", "full_name", "created_at", "status", "alert_id"}
    assert crit[0]["full_name"] == "Name P-9001" and crit[0]["alert_id"]


def test_clinic_detail_falls_back_to_the_id_without_a_studies_row(call_staff, clinic_data):
    status, body = call_staff("GET", "/staff/clinic/ZZZ-999")
    assert status == 200, body
    assert body["study"] == {"study_id": "ZZZ-999", "name": "ZZZ-999", "phase": None, "condition": None,
                             "description": None}
    assert body["critical_list"] == [] and body["engagement"] == {"engaged": 0, "not_engaging": 1}
    assert [d["total"] for d in body["symptoms_by_day"]] == [0] * 14


def test_clinic_agent_handled_vs_critical_split(call_staff, admin, studies):
    _participant(admin, "P-9101", "HTN-205")
    _symptom(admin, "P-9101", {"hours": 1})                                          # handled
    _symptom(admin, "P-9101", {"hours": 2}, hint="severe")                           # handled (severe is not paged)
    _symptom(admin, "P-9101", {"hours": 3}, status="escalated", hint="urgent")       # paged
    _symptom(admin, "P-9101", {"hours": 4}, status="logged", hint="urgent")          # urgent severity alone pages
    _alert(admin, "P-9101", {"hours": 3})
    _alert(admin, "P-9101", {"hours": 2}, status="acknowledged")
    _alert(admin, "P-9101", {"hours": 1}, status="resolved")
    status, body = call_staff("GET", "/staff/clinic")
    trial = body["trials"][0]
    assert (trial["symptoms"], trial["agent_handled"], trial["critical"]) == (4, 2, 3)
    assert body["summary"]["critical_open"] == 2                  # open + acknowledged, not resolved
    status, body = call_staff("GET", "/staff/clinic/HTN-205")
    assert sum(d["total"] for d in body["symptoms_by_day"]) == 4
    assert sum(d["critical"] for d in body["symptoms_by_day"]) == 2


def test_clinic_critical_list_is_the_newest_20(call_staff, admin, studies):
    _participant(admin, "P-9201", "DM2-410")
    for i in range(25):
        _alert(admin, "P-9201", {"minutes": 10 + i})
    status, body = call_staff("GET", "/staff/clinic/DM2-410")
    assert status == 200 and len(body["critical_list"]) == 20
    assert body["summary"]["critical"] == 25
    created = [c["created_at"] for c in body["critical_list"]]
    assert created == sorted(created, reverse=True)


def test_clinic_unknown_study_is_404(call_staff, clinic_data):
    for sid in ("NOPE-000", "koa-301", "KOA-301%27"):
        status, body = call_staff("GET", f"/staff/clinic/{sid}")
        assert status == 404 and body == {"error": "Trial not found"}, sid


def test_ask_hookup_never_crashes_with_the_real_handler(call_staff):
    status, body = call_staff("POST", "/staff/ask", {})
    assert status != 500
    if status >= 400:
        assert "error" in body                                    # 404 while ask.py is absent, else its own 4xx
