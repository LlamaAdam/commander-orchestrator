"""Smoke test for the dirty-tree-safe branch/commit/revert helpers.

Creates a throwaway git repo, simulates a developer with unrelated
work-in-progress (an untracked file + a modified tracked file), and
verifies that the auto-fix branch lifecycle:

  1. succeeds despite unrelated dirt,
  2. commits ONLY the patched file (never the user's WIP),
  3. on revert, preserves the user's untracked + modified files,
  4. refuses when the TARGET file itself is dirty.

Run: python scripts/smoke_dirty_tree.py
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from orchestrator.auto_fix import (
    create_working_branch, commit_files, revert_files, apply_diff, _dirty_paths,
)


def _git(args, cwd):
    return subprocess.run(["git"] + args, cwd=str(cwd), capture_output=True,
                          text=True, encoding="utf-8")


def _init_repo(d: Path):
    # NOTE: deliberately does NOT configure user.name/user.email on the repo.
    # This mirrors the real commander-builder clone (no git identity), so the
    # test proves commit_files' inline identity actually works.
    _git(["init", "-q"], d)
    _git(["checkout", "-b", "main"], d)
    (d / "target.py").write_text("value = 1\n", encoding="utf-8")
    (d / "other.py").write_text("kept = True\n", encoding="utf-8")
    _git(["add", "-A"], d)
    # The seed commit still needs SOME identity; commit_files supplies its own.
    _git(["-c", "user.email=seed@t", "-c", "user.name=seed", "commit", "-q", "-m", "init"], d)


def _commit_env(d, args):
    # No-op: identity intentionally left unset so commit_files must provide its own.
    pass


def case_unrelated_wip_preserved():
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        _init_repo(d)
        _commit_env(d, None)

        # Developer WIP: an untracked file + a modified tracked file.
        (d / "wip_untracked.py").write_text("scratch = 42\n", encoding="utf-8")
        (d / "other.py").write_text("kept = True\nwip_edit = 1\n", encoding="utf-8")

        # Auto-fix wants to patch target.py (value 1 -> 2).
        diff = (
            "--- a/target.py\n"
            "+++ b/target.py\n"
            "@@ -1 +1 @@\n"
            "-value = 1\n"
            "+value = 2\n"
        )
        target = ["target.py"]

        br = create_working_branch(d, "auto-fix/test", target_files=target)
        assert br.success, f"branch create should succeed despite unrelated dirt: {br.error}"

        ar = apply_diff(diff, d)
        assert ar.success, f"diff should apply: {ar.error}"

        cr = commit_files(d, "auto-fix: target", target)
        assert cr.success, f"commit should succeed: {cr.error}"

        # The auto-fix commit must contain ONLY target.py.
        show = _git(["show", "--name-only", "--format=", "HEAD"], d).stdout.split()
        assert show == ["target.py"], f"commit should touch only target.py, got {show}"

        # User WIP must still be present in the working tree.
        assert (d / "wip_untracked.py").exists(), "untracked WIP destroyed!"
        assert "wip_edit = 1" in (d / "other.py").read_text(), "modified WIP lost!"
        print("  case 1 PASS: unrelated WIP preserved; only target.py committed")


def case_revert_preserves_wip():
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        _init_repo(d)
        _commit_env(d, None)

        (d / "wip_untracked.py").write_text("scratch = 99\n", encoding="utf-8")

        # A diff that applies but we'll then "revert" (simulating regression).
        diff = (
            "--- a/target.py\n"
            "+++ b/target.py\n"
            "@@ -1 +1 @@\n"
            "-value = 1\n"
            "+value = 999\n"
        )
        target = ["target.py"]

        create_working_branch(d, "auto-fix/test2", target_files=target)
        apply_diff(diff, d)
        # Simulate regression -> revert.
        rv = revert_files(d, target, "main")
        assert rv.success, f"revert should succeed: {rv.error}"

        # target.py back to committed state, WIP intact.
        assert (d / "target.py").read_text() == "value = 1\n", "target.py not reverted"
        assert (d / "wip_untracked.py").exists(), "revert destroyed untracked WIP!"
        assert (d / "wip_untracked.py").read_text() == "scratch = 99\n", "WIP content changed"
        print("  case 2 PASS: revert restored target.py, preserved untracked WIP")


def case_target_conflict_refused():
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        _init_repo(d)
        _commit_env(d, None)

        # The TARGET file itself is dirty -- must refuse.
        (d / "target.py").write_text("value = 1\nuser_was_here = 1\n", encoding="utf-8")

        br = create_working_branch(d, "auto-fix/test3", target_files=["target.py"])
        assert not br.success, "should refuse when target file is dirty"
        assert "uncommitted changes" in br.error, f"unexpected error: {br.error}"
        print("  case 3 PASS: refused branch when target file had WIP")


def case_dirty_paths_parser():
    sample = " M src/foo.py\n?? bar.py\nA  baz.py\nR  old.py -> new.py\n"
    got = _dirty_paths(sample)
    assert got == {"src/foo.py", "bar.py", "baz.py", "new.py"}, got
    print("  case 4 PASS: _dirty_paths parsed porcelain correctly")


def main():
    print("[smoke_dirty_tree]")
    case_dirty_paths_parser()
    case_unrelated_wip_preserved()
    case_revert_preserves_wip()
    case_target_conflict_refused()
    print("[smoke_dirty_tree] all cases PASS")


if __name__ == "__main__":
    main()
