"""orch-launcher — config persistence + scriptable dispatch (no subprocess)."""
from __future__ import annotations

from pathlib import Path

from orchestrator import launcher as L


def _isolate(monkeypatch, tmp_path):
    """Point the launcher's project root + config at a tmp dir."""
    monkeypatch.setattr(L, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(L, "CONFIG_PATH", tmp_path / "data" / "launcher_config.json")


def test_load_config_defaults_when_missing(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    cfg = L.load_config()
    assert cfg["repo_dir"] == "data/repos/commander-builder"
    assert cfg["burn_ceiling"] == 5.0


def test_save_then_load_roundtrips(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    cfg = L.load_config()
    cfg["repo_dir"] = r"C:\dev\some-repo"
    cfg["hours"] = 3.0
    L.save_config(cfg)
    again = L.load_config()
    assert again["repo_dir"] == r"C:\dev\some-repo"
    assert again["hours"] == 3.0


def test_corrupt_config_falls_back_to_defaults(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    L.CONFIG_PATH.parent.mkdir(parents=True)
    L.CONFIG_PATH.write_text("{not json", encoding="utf-8")
    cfg = L.load_config()  # no exception
    assert cfg["repo_dir"] == "data/repos/commander-builder"


def test_resolve_repo_dir_relative_vs_absolute(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    rel = L.resolve_repo_dir({"repo_dir": "data/repos/x"})
    assert rel == tmp_path / "data" / "repos" / "x"
    abs_in = str(tmp_path / "elsewhere")
    assert L.resolve_repo_dir({"repo_dir": abs_in}) == Path(abs_in)


def test_describe_repo_states(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    assert "MISSING" in L.describe_repo({"repo_dir": "nope"})

    plain = tmp_path / "plain"
    plain.mkdir()
    assert "not a git repo" in L.describe_repo({"repo_dir": str(plain)})

    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    assert "ready" in L.describe_repo({"repo_dir": str(repo)})


def test_set_repo_subcommand_persists(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    rc = L.main(["set-repo", str(tmp_path / "target")])
    assert rc == 0
    assert L.load_config()["repo_dir"] == str(tmp_path / "target")


def test_show_subcommand_runs(monkeypatch, tmp_path, capsys):
    _isolate(monkeypatch, tmp_path)
    assert L.main(["show"]) == 0
    out = capsys.readouterr().out
    assert "target repo" in out


def test_audit_subcommand_dispatches_to_orch(monkeypatch, tmp_path):
    """`orch-launcher audit` -> `orch audit --repo-dir <resolved>` (no spawn)."""
    _isolate(monkeypatch, tmp_path)
    L.main(["set-repo", str(tmp_path / "tr")])
    seen = {}

    def fake_run(args):
        seen["args"] = args
        return 0
    monkeypatch.setattr(L, "_run", fake_run)

    assert L.main(["audit"]) == 0
    a = seen["args"]
    assert a[1:4] == ["-m", "orchestrator.cli", "audit"]
    assert "--repo-dir" in a
    assert a[a.index("--repo-dir") + 1] == str(tmp_path / "tr")


def test_run_subcommand_invokes_continuous(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    seen = {}

    def fake_run(args):
        seen["args"] = args
        return 0
    monkeypatch.setattr(L, "_run", fake_run)

    assert L.main(["run", "--hours", "2"]) == 0
    a = seen["args"]
    assert str(L.PROJECT_ROOT / "scripts" / "run_continuous.py") in a
    assert a[a.index("--hours") + 1] == "2.0"
