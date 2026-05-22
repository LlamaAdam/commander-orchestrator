"""auto_fix branch lifecycle — WIP-safe create/commit/revert against a real
temp git repo. These guard the rule: the orchestrator must NEVER stage,
commit, or destroy a developer's unrelated work-in-progress."""
from __future__ import annotations

from pathlib import Path

from orchestrator import auto_fix as af


def _porcelain(repo, git_helper):
    return git_helper(["status", "--porcelain"], cwd=repo).stdout


def test_current_branch(git_repo):
    assert af.current_branch(git_repo) == "main"


def test_create_working_branch_clean(git_repo):
    r = af.create_working_branch(git_repo, "auto/fix")
    assert r.success
    assert af.current_branch(git_repo) == "auto/fix"


def test_create_working_branch_refuses_dirty_target(git_repo):
    (git_repo / "src.py").write_text("x = 999  # WIP edit\n", encoding="utf-8")
    r = af.create_working_branch(git_repo, "auto/fix", target_files=["src.py"])
    assert r.success is False
    assert "WIP" in r.error
    # Did NOT switch branches — still on main, WIP intact.
    assert af.current_branch(git_repo) == "main"
    assert "999" in (git_repo / "src.py").read_text(encoding="utf-8")


def test_create_working_branch_allows_unrelated_dirt(git_repo):
    # src.py is dirty, but the diff only targets the test file -> allowed.
    (git_repo / "src.py").write_text("x = 2  # unrelated WIP\n", encoding="utf-8")
    r = af.create_working_branch(git_repo, "auto/fix", target_files=["tests/test_x.py"])
    assert r.success
    assert af.current_branch(git_repo) == "auto/fix"
    # The unrelated WIP is carried along, not lost.
    assert "unrelated WIP" in (git_repo / "src.py").read_text(encoding="utf-8")


def test_commit_files_stages_only_given_and_works_without_config(git_repo, git_helper):
    af.create_working_branch(git_repo, "auto/fix")
    (git_repo / "src.py").write_text("x = 42  # the fix\n", encoding="utf-8")
    (git_repo / "unrelated.py").write_text("# user's untracked WIP\n", encoding="utf-8")

    r = af.commit_files(git_repo, "auto-fix: patch src", ["src.py"])
    assert r.success, r.error

    # The committed file shows in the last commit; unrelated.py was NOT swept in.
    names = git_helper(["show", "--name-only", "--format=", "HEAD"], cwd=git_repo).stdout
    assert "src.py" in names
    assert "unrelated.py" not in names
    # unrelated.py is still an untracked WIP file.
    assert "?? unrelated.py" in _porcelain(git_repo, git_helper)
    # Inline synthetic identity was used (no host git config needed).
    author = git_helper(["log", "-1", "--format=%ae"], cwd=git_repo).stdout.strip()
    assert author == "orchestrator@local"


def test_commit_files_empty_list_errors(git_repo):
    af.create_working_branch(git_repo, "auto/fix")
    r = af.commit_files(git_repo, "noop", [])
    assert r.success is False


_DIFF = """diff --git a/src.py b/src.py
--- a/src.py
+++ b/src.py
@@ -1 +1 @@
-x = 1
+x = 2
"""


def test_apply_diff_works_with_relative_repo_dir(git_repo, monkeypatch):
    """Regression: apply_diff must use an ABSOLUTE patch path. With a relative
    repo_dir (e.g. 'data/repos/x'), git runs with cwd=repo_dir and would
    misresolve a cwd-relative patch path -> 'can't open patch'. Surfaced by
    dogfooding tier-2 (Claude's diff failed to apply)."""
    # cwd is the repo's PARENT; repo_dir is passed as a RELATIVE name.
    monkeypatch.chdir(git_repo.parent)
    res = af.apply_diff(_DIFF, Path(git_repo.name))
    assert res.success, res.error
    assert (git_repo / "src.py").read_text(encoding="utf-8") == "x = 2\n"


def test_apply_diff_empty_is_error(git_repo):
    assert af.apply_diff("   ", git_repo).success is False


def test_sanitize_diff_strips_markdown_fence():
    fenced = "Here is the fix:\n```diff\n" + _DIFF + "```\nHope that helps!"
    out = af.sanitize_diff(fenced)
    assert out.startswith("diff --git a/src.py b/src.py")
    assert "Here is the fix" not in out and "```" not in out
    assert out.endswith("\n")


def test_sanitize_diff_strips_leading_prose():
    out = af.sanitize_diff("Sure!\nThe patch:\n" + _DIFF)
    assert out.startswith("diff --git")


def test_apply_diff_accepts_fenced_llm_diff(git_repo):
    """A diff wrapped in a ```diff fence with prose (classic LLM output) must
    still apply after sanitizing -- this is bug #5 from the dogfood."""
    fenced = "Here's the minimal fix:\n\n```diff\n" + _DIFF + "```\n"
    res = af.apply_diff(fenced, git_repo)
    assert res.success, res.error
    assert (git_repo / "src.py").read_text(encoding="utf-8") == "x = 2\n"


def test_apply_diff_recounts_wrong_hunk_header(git_repo):
    """LLMs routinely miscount @@ line numbers. The --recount fallback should
    still apply a diff whose hunk header counts are wrong."""
    bad_counts = (
        "diff --git a/src.py b/src.py\n"
        "--- a/src.py\n"
        "+++ b/src.py\n"
        "@@ -1,5 +1,5 @@\n"   # wrong counts (file is only 1 line)
        "-x = 1\n"
        "+x = 2\n"
    )
    res = af.apply_diff(bad_counts, git_repo)
    assert res.success, res.error
    assert (git_repo / "src.py").read_text(encoding="utf-8") == "x = 2\n"


def test_apply_diff_repairs_headerless_hunk(git_repo, git_helper):
    """Bug #5 last layer (from the dogfood): LLMs emit hunk headers WITHOUT
    line ranges -- '@@ def f(x): @@' instead of '@@ -a,b +c,d @@' -- which git
    rejects as 'patch with only garbage'. sanitize_diff rewrites the header to
    a placeholder and --recount applies it via context matching."""
    mod = git_repo / "mod.py"
    mod.write_text("def f(x):\n    status = 'a'\n    delta = x - 1\n    return delta\n",
                   encoding="utf-8")
    git_helper(["add", "mod.py"], cwd=git_repo)
    git_helper(["commit", "-m", "add mod"], cwd=git_repo)
    bad = (
        "diff --git a/mod.py b/mod.py\n"
        "--- a/mod.py\n"
        "+++ b/mod.py\n"
        "@@ def f(x): @@\n"            # malformed: no -a,b +c,d ranges
        "     status = 'a'\n"
        "-    delta = x - 1\n"
        "+    delta = x + 1\n"
        "     return delta\n"
    )
    res = af.apply_diff(bad, git_repo)
    assert res.success, res.error
    assert "delta = x + 1" in mod.read_text(encoding="utf-8")


def test_sanitize_diff_normalizes_headerless_hunk():
    out = af.sanitize_diff("--- a/x.py\n+++ b/x.py\n@@ def foo(): @@\n-a\n+b\n")
    assert "@@ -1 +1 @@" in out
    assert "@@ def foo()" not in out


def test_apply_diff_failure_includes_preview(git_repo):
    res = af.apply_diff("diff --git a/nope.py b/nope.py\n--- a/nope.py\n+++ b/nope.py\n"
                        "@@ -1 +1 @@\n-was\n+now\n", git_repo)
    assert res.success is False
    assert "diff preview:" in res.error


def test_check_no_test_weakening_against_committed(git_repo):
    # git_repo ships tests/test_x.py = "def test_x():\n    assert True\n".
    # Unchanged working tree -> no weakening.
    assert af.check_no_test_weakening(git_repo, ["tests/test_x.py"]) == ""
    # Gut the assertion in the working tree -> flagged vs the committed version.
    (git_repo / "tests" / "test_x.py").write_text("def test_x():\n    pass\n", encoding="utf-8")
    reason = af.check_no_test_weakening(git_repo, ["tests/test_x.py"])
    assert "assertions removed" in reason


def test_check_no_test_weakening_ignores_source_files(git_repo):
    # src.py is NOT a test file -> never checked, even if assertions vanish.
    (git_repo / "src.py").write_text("x = 1\n", encoding="utf-8")
    assert af.check_no_test_weakening(git_repo, ["src.py"]) == ""


def test_revert_files_restores_target_and_preserves_wip(git_repo):
    af.create_working_branch(git_repo, "auto/fix")
    # A failed "patch" to src.py (uncommitted) + a user's untracked WIP file.
    (git_repo / "src.py").write_text("x = BROKEN\n", encoding="utf-8")
    (git_repo / "wip.py").write_text("# precious untracked work\n", encoding="utf-8")

    r = af.revert_files(git_repo, ["src.py"], "main")
    assert r.success, r.error
    assert af.current_branch(git_repo) == "main"
    # Patched file restored to its committed state...
    assert (git_repo / "src.py").read_text(encoding="utf-8") == "x = 1\n"
    # ...but the user's untracked WIP survived (no clean -fd / reset --hard).
    assert (git_repo / "wip.py").exists()
