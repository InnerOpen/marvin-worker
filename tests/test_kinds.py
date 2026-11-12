"""A worker with no kinds, or a pack that will not load, says what to do; `kinds` shows what is loaded; the example pack works."""

import sys
from pathlib import Path

import pytest

from marvin_worker import jobs, runner, serve

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "hello"


@pytest.fixture(autouse=True)
def clean_registry(monkeypatch):
    """Each test starts with no kinds and no modules, and its imports are forgotten after."""
    monkeypatch.setattr(jobs, "HANDLERS", {})
    monkeypatch.setattr(jobs, "META", {})
    for var in ("WORKER_JOB_MODULES", "WORKER_KINDS"):
        monkeypatch.delenv(var, raising=False)
    before = set(sys.modules)
    yield
    for name in set(sys.modules) - before:
        if name.startswith(("hello_pack", "broken_pack")):
            del sys.modules[name]


def test_a_module_that_will_not_import_is_named_with_the_usual_causes(monkeypatch):
    monkeypatch.setenv("WORKER_JOB_MODULES", "no_such_pack.jobs")
    with pytest.raises(jobs.KindsError) as e:
        jobs.load_kinds()
    said = str(e.value)
    assert "“no_such_pack.jobs”" in said and "ModuleNotFoundError" in said and "PYTHONPATH" in said and "__init__.py" in said


def test_kinds_with_nothing_loaded_says_how_to_add_one_and_exits_with_an_error(capsys):
    assert jobs.kinds_command() == 2
    out = capsys.readouterr().out
    assert "WORKER_JOB_MODULES = (not set)" in out and "ships none on purpose" in out and "examples/hello" in out and "docs/adding-kinds.md" in out


def test_the_example_pack_loads_shows_its_contract_and_runs(monkeypatch, capsys):
    monkeypatch.syspath_prepend(str(EXAMPLE))
    monkeypatch.setenv("WORKER_JOB_MODULES", "hello_pack.jobs")
    assert jobs.kinds_command() == 0
    out = capsys.readouterr().out
    assert "1 kind offered" in out and "hello  -  Greet someone" in out and "takes: name (string), shout (boolean, optional)" in out
    ctx = jobs.Context(creds=("http://m", "tok"), work=Path("."), name="hello-1")
    assert jobs.HANDLERS["hello"]({"args": {"name": "Ada"}}, ctx)["result"] == {"greeting": "Hello, Ada!", "letters": 3}
    assert jobs.HANDLERS["hello"]({"args": {"name": "Ada", "shout": True}}, ctx)["log"] == "HELLO, ADA!"
    jobs.check_args("hello", {"name": "Ada"})  # `shout` is optional: no complaint
    with pytest.raises(jobs.JobError, match="“name” is needed"):  # and `name` is needed
        jobs.check_args("hello", {})


def test_a_kind_whose_tool_is_missing_is_flagged(monkeypatch, capsys, tmp_path):
    pack = tmp_path / "broken_pack"
    pack.mkdir()
    (pack / "__init__.py").write_text("")
    (pack / "jobs.py").write_text('from marvin_worker.jobs import job_kind\n\n@job_kind("cut", needs=["definitely-not-a-program"])\ndef cut(job, ctx):\n    return {}\n')
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv("WORKER_JOB_MODULES", "broken_pack.jobs")
    assert jobs.kinds_command() == 0
    out = capsys.readouterr().out
    assert "needs: definitely-not-a-program  <- NOT FOUND" in out and "warning: not found here: definitely-not-a-program" in out


def test_worker_kinds_that_names_none_of_the_loaded_ones_is_explained(monkeypatch, capsys):
    monkeypatch.syspath_prepend(str(EXAMPLE))
    monkeypatch.setenv("WORKER_JOB_MODULES", "hello_pack.jobs")
    monkeypatch.setenv("WORKER_KINDS", "render")
    assert jobs.kinds_command() == 2
    assert "1 kind(s) are loaded (hello) but WORKER_KINDS names none of them" in capsys.readouterr().out


def test_a_runner_and_a_standing_worker_with_no_kinds_stop_with_the_same_guidance(monkeypatch, capsys):
    for var, value in (("MARVIN_API_URL", "http://m"), ("MARVIN_USER_TOKEN", "tok"), ("WORKER_TOKEN", "w")):
        monkeypatch.setenv(var, value)
    assert runner.main() == 2 and "ships none on purpose" in capsys.readouterr().out
    assert serve.main() == 2 and "docs/adding-kinds.md" in capsys.readouterr().out
    monkeypatch.setenv("WORKER_JOB_MODULES", "no_such_pack.jobs")
    assert runner.main() == 2 and "could not be imported" in capsys.readouterr().out
