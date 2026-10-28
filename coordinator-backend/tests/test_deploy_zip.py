"""The Lambda zips bundle the OTel SDK and common/telemetry.py, and building them touches no AWS.

The deploy scripts import boto3 at module level; here boto3 is replaced by a stub that raises on any client or
session, so these tests fail if a zip build (or an import) ever reaches for AWS. pip is never run for real: the
dependency install is replaced by copying the locally installed opentelemetry package, and the pip command line
is checked separately.
"""

import importlib.util
import shutil
import subprocess
import sys
import types
from pathlib import Path
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[2]
TRIGGERS_INFRA = ROOT / "triggers" / "infra"
PORTAL_INFRA = ROOT / "coordinator-backend" / "infra"


class _NoAws(types.ModuleType):
    def _refuse(self, *a, **k):
        raise AssertionError("a deploy zip build must not construct an AWS client or session")

    client = resource = Session = _refuse


def _stub_aws(monkeypatch):
    monkeypatch.setitem(sys.modules, "boto3", _NoAws("boto3"))
    try:
        import botocore.exceptions  # noqa: F401
    except ImportError:
        botocore = types.ModuleType("botocore")
        exc = types.ModuleType("botocore.exceptions")
        exc.ClientError = type("ClientError", (Exception,), {})
        botocore.exceptions = exc
        monkeypatch.setitem(sys.modules, "botocore", botocore)
        monkeypatch.setitem(sys.modules, "botocore.exceptions", exc)


def _load(key, path):
    spec = importlib.util.spec_from_file_location(f"_test_deploy_{key}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def deploy(monkeypatch):
    """(triggers deploy, portal deploy), imported with a raising boto3 and a fake pip install."""
    _stub_aws(monkeypatch)
    monkeypatch.syspath_prepend(str(TRIGGERS_INFRA))
    monkeypatch.delitem(sys.modules, "deploy", raising=False)
    trig = _load("triggers", TRIGGERS_INFRA / "deploy.py")
    portal = _load("portal", PORTAL_INFRA / "deploy_portal.py")
    shared = sys.modules["deploy"]      # what deploy_portal imported from
    otel = pytest.importorskip("opentelemetry")

    def fake_install(staging):          # stands in for `pip install -r infra-requirements-otel.txt -t <staging>`
        shutil.copytree(Path(otel.__path__[0]), Path(staging) / "opentelemetry")

    for m in (trig, shared):
        monkeypatch.setattr(m, "install_otel_deps", fake_install)
    yield trig, portal
    sys.modules.pop("deploy", None)


def names(path):
    with zipfile.ZipFile(path) as z:
        return set(z.namelist())


def test_portal_zip_has_otel_telemetry_and_handler(deploy, tmp_path):
    _, portal = deploy
    zip_path = portal.build_zip(tmp_path)
    assert isinstance(zip_path, Path) and zip_path.exists()
    n = names(zip_path)
    assert any(x.startswith("opentelemetry/") for x in n)
    assert "common/telemetry.py" in n
    assert "staff_api/handler.py" in n
    assert not any("__pycache__" in x for x in n)
    assert not any(x.startswith("migrate/") for x in n)


def test_trigger_zip_has_otel_telemetry_and_handler(deploy, tmp_path):
    trig, _ = deploy
    n = names(trig.build_zip("tt-router", tmp_path))
    assert any(x.startswith("opentelemetry/") for x in n)
    assert "handler.py" in n and "telemetry.py" in n   # the trigger Lambdas are deployed flat
    assert not any("__pycache__" in x for x in n)


def test_prebuilt_deps_dir_is_reused(deploy, tmp_path):
    trig, _ = deploy
    deps = tmp_path / "deps"
    (deps / "opentelemetry").mkdir(parents=True)
    (deps / "opentelemetry" / "marker.py").write_text("x = 1\n")
    trig.install_otel_deps = lambda staging: pytest.fail("deps_dir was given, nothing should be installed")
    assert "opentelemetry/marker.py" in names(trig.build_zip("tt-ingest", tmp_path, deps_dir=deps))


def test_functions_get_the_db_sink_env_var(deploy):
    trig, portal = deploy
    assert trig.TELEMETRY_ENV == {"TELEMETRY_DB_SINK": "1"}
    assert portal.TELEMETRY_ENV == {"TELEMETRY_DB_SINK": "1"}


def test_pip_command_uses_the_pinned_file(monkeypatch, tmp_path):
    _stub_aws(monkeypatch)
    monkeypatch.syspath_prepend(str(TRIGGERS_INFRA))
    monkeypatch.delitem(sys.modules, "deploy", raising=False)
    trig = _load("triggers_pip", TRIGGERS_INFRA / "deploy.py")
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: calls.append((cmd, kw)))
    trig.install_otel_deps(tmp_path / "staging")
    (cmd, kw), = calls
    assert cmd[1:4] == ["-m", "pip", "install"]
    assert cmd[cmd.index("-r") + 1] == str(ROOT / "infra-requirements-otel.txt")
    assert cmd[cmd.index("-t") + 1] == str(tmp_path / "staging")
    assert cmd[cmd.index("--python-version") + 1] == "3.12"
    assert "--only-binary=:all:" in cmd and kw.get("check") is True


def test_requirements_pin_otel_exactly():
    lines = {line.strip() for line in (ROOT / "infra-requirements-otel.txt").read_text().splitlines()}
    assert "opentelemetry-api==1.45.1" in lines and "opentelemetry-sdk==1.45.1" in lines
