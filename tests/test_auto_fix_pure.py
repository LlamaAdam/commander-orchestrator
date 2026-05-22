"""auto_fix pure logic: parsers, danger/test-file gates, dedup, graduation."""
from __future__ import annotations

from orchestrator import auto_fix as af
from conftest import make_failure


# --- parse_fix_action -------------------------------------------------------

def test_parse_fenced_json_install():
    fa = af.parse_fix_action('```json\n{"action":"install_package","confidence":0.9,"package":"flask"}\n```')
    assert fa.action == "install_package" and fa.package == "flask" and fa.confidence == 0.9


def test_parse_bare_json_apply_diff():
    fa = af.parse_fix_action('{"action":"apply_diff","diff":"--- a\\n+++ b\\n","files_touched":["tests/test_x.py"]}')
    assert fa.action == "apply_diff"
    assert fa.files_touched == ["tests/test_x.py"]


def test_parse_loose_json_amid_prose():
    fa = af.parse_fix_action('Sure, here you go: {"action": "escalate", "escalate_reason": "too hard"} -- hope that helps')
    assert fa.action == "escalate"


def test_parse_non_json_becomes_escalate():
    fa = af.parse_fix_action("I think you should just install flask?")
    assert fa.action == "escalate"
    assert "not parseable" in fa.escalate_reason


def test_parse_unknown_action_becomes_escalate():
    fa = af.parse_fix_action('{"action": "rm_rf_slash", "confidence": 1.0}')
    assert fa.action == "escalate"
    assert "unknown action" in fa.escalate_reason


# --- _safe_package_spec -----------------------------------------------------

def test_safe_package_spec_accepts_normal():
    for spec in ("flask", "commander-builder[web]", "flask==2.0.1", "numpy>=1.20", "pytest_cov"):
        assert af._safe_package_spec(spec), spec


def test_safe_package_spec_rejects_injection():
    for spec in ("flask; rm -rf /", "../evil", "flask && curl x", "", "pkg|cat", "a b"):
        assert not af._safe_package_spec(spec), spec


# --- danger + test-file gates ----------------------------------------------

def test_is_danger_path_flags_config_and_secrets():
    pats = af.DEFAULT_DANGER_PATTERNS
    for p in ("pyproject.toml", "setup.py", ".env", "app/migrations/0001.py",
              "src/auth/login.py", ".github/workflows/ci.yml"):
        assert af.is_danger_path(p, pats), p


def test_is_danger_path_allows_normal_source():
    pats = af.DEFAULT_DANGER_PATTERNS
    for p in ("src/commander_builder/foo.py", "tests/test_foo.py", "README.md"):
        assert not af.is_danger_path(p, pats), p


def test_is_test_file_recognizes_tests():
    for p in ("tests/test_x.py", "pkg/tests/test_y.py", "test_z.py",
              "conftest.py", "foo_test.py"):
        assert af._is_test_file(p), p


def test_is_test_file_rejects_source():
    for p in ("src/foo.py", "pkg/module.py", "main.py"):
        assert not af._is_test_file(p), p


def test_load_danger_list_defaults_when_missing(tmp_path):
    assert af.load_danger_list(tmp_path) == list(af.DEFAULT_DANGER_PATTERNS)


def test_load_danger_list_reads_file(tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    (d / "danger_list.txt").write_text("# comment\ncustom/**\n\nsecrets.py\n", encoding="utf-8")
    assert af.load_danger_list(tmp_path) == ["custom/**", "secrets.py"]


# --- dedup ------------------------------------------------------------------

def test_dedup_hash_stable_and_distinct():
    f1 = make_failure(nodeid="t::a", traceback="boom A")
    f1b = make_failure(nodeid="t::a", traceback="boom A")
    f2 = make_failure(nodeid="t::b", traceback="boom A")
    assert af._dedup_hash(f1) == af._dedup_hash(f1b)
    assert af._dedup_hash(f1) != af._dedup_hash(f2)


def test_seen_roundtrip_and_corrupt(tmp_path):
    p = tmp_path / "seen.json"
    af.save_seen(p, {"abc": {"attempt_count": 2}})
    assert af.load_seen(p) == {"abc": {"attempt_count": 2}}
    p.write_text("not json", encoding="utf-8")
    assert af.load_seen(p) == {}            # corrupt -> empty, no raise
    assert af.load_seen(tmp_path / "nope.json") == {}  # missing -> empty


# --- _dirty_paths -----------------------------------------------------------

def test_dirty_paths_parses_porcelain_with_rename():
    porcelain = " M src/a.py\n?? new.py\nR  old.py -> renamed.py\nA  added.py\n"
    paths = af._dirty_paths(porcelain)
    assert "src/a.py" in paths
    assert "new.py" in paths
    assert "renamed.py" in paths and "old.py" not in paths
    assert "added.py" in paths


# --- graduation -------------------------------------------------------------

def test_record_verified_success_crosses_threshold():
    state = {}
    crossed = [af.record_verified_success(state, "install_package", threshold=3)
               for _ in range(3)]
    assert crossed == [False, False, True]   # 3rd call graduates
    assert af.is_graduated(state, "install_package") is True
    # Already graduated -> no further crossings.
    assert af.record_verified_success(state, "install_package", threshold=3) is False


def test_is_graduated_false_for_unknown():
    assert af.is_graduated({}, "apply_diff") is False


def test_graduation_state_roundtrip(tmp_path):
    p = tmp_path / "grad.json"
    state = {"install_package": {"successes": 4, "graduated": False}}
    af.save_graduation(p, state)
    assert af.load_graduation(p) == state
    assert af.load_graduation(tmp_path / "missing.json") == {}


# --- parse_verification -----------------------------------------------------

def test_parse_verification_approve():
    v = af.parse_verification('{"approve": true, "reason": "looks correct"}')
    assert v.approve is True and "correct" in v.reason


def test_parse_verification_reject():
    v = af.parse_verification('```json\n{"approve": false, "reason": "too broad"}\n```')
    assert v.approve is False


def test_parse_verification_unparseable_is_reject():
    v = af.parse_verification("I approve this fix!")
    assert v.approve is False
    assert "unparseable" in v.reason


# --- needs_human queue: structured index, dedup, resolve, render ------------

def test_needs_human_append_creates_open_entry(tmp_path):
    f = make_failure(nodeid="tests/test_a.py::test_x")
    md = af.append_needs_human(tmp_path, failure=f, action=None, reason="stuck")
    idx = af.load_needs_human_index(tmp_path)
    assert idx["tests/test_a.py::test_x"]["status"] == "open"
    assert "tests/test_a.py::test_x" in md.read_text(encoding="utf-8")


def test_needs_human_dedups_by_nodeid(tmp_path):
    f = make_failure(nodeid="tests/test_a.py::test_x")
    af.append_needs_human(tmp_path, failure=f, action=None, reason="try 1")
    af.append_needs_human(tmp_path, failure=f, action=None, reason="try 2")
    idx = af.load_needs_human_index(tmp_path)
    # ONE entry, not two; count bumped; latest reason kept.
    assert len(idx) == 1
    entry = idx["tests/test_a.py::test_x"]
    assert entry["escalation_count"] == 2
    assert entry["reason"] == "try 2"


def test_needs_human_resolve_clears_from_open_view(tmp_path):
    f = make_failure(nodeid="tests/test_a.py::test_x")
    md = af.append_needs_human(tmp_path, failure=f, action=None, reason="stuck")
    assert "tests/test_a.py::test_x" in md.read_text(encoding="utf-8")

    assert af.resolve_needs_human(tmp_path, nodeid="tests/test_a.py::test_x") is True
    idx = af.load_needs_human_index(tmp_path)
    assert idx["tests/test_a.py::test_x"]["status"] == "resolved"
    # The rendered .md (open-only) no longer lists it.
    assert "tests/test_a.py::test_x" not in md.read_text(encoding="utf-8")
    # Resolving again is a no-op.
    assert af.resolve_needs_human(tmp_path, nodeid="tests/test_a.py::test_x") is False


def test_needs_human_render_shows_only_open(tmp_path):
    af.append_needs_human(tmp_path, failure=make_failure(nodeid="t::open"), action=None, reason="r1")
    af.append_needs_human(tmp_path, failure=make_failure(nodeid="t::done"), action=None, reason="r2")
    af.resolve_needs_human(tmp_path, nodeid="t::done")
    text = af.needs_human_md_path(tmp_path).read_text(encoding="utf-8")
    assert "t::open" in text and "t::done" not in text
    assert "1 open, 1 resolved" in text


# --- test-weakening detector ------------------------------------------------

def test_detect_weakening_flags_removed_assertion():
    before = "def t():\n    assert a == 1\n    assert b == 2\n"
    after = "def t():\n    assert a == 1\n"
    assert af.detect_test_weakening(before, after) == "assertions removed"


def test_detect_weakening_flags_added_skip():
    before = "def t():\n    assert a\n"
    after = "import pytest\n@pytest.mark.skip\ndef t():\n    assert a\n"
    assert "skip/xfail" in af.detect_test_weakening(before, after)


def test_detect_weakening_allows_genuine_changes():
    before = "def t():\n    assert a == 1\n"
    # Same assertion count, value changed (could be a legit test correction) -> allowed.
    assert af.detect_test_weakening(before, "def t():\n    assert a == 2\n") == ""
    # Adding assertions is never weakening.
    assert af.detect_test_weakening(before, "def t():\n    assert a == 1\n    assert c\n") == ""


def test_count_assertions_recognizes_unittest_and_raises():
    txt = "self.assertEqual(x, 1)\nwith pytest.raises(ValueError):\n    assert y\n"
    assert af._count_assertions(txt) == 3


def test_needs_human_archives_legacy_md_once(tmp_path):
    # A pre-existing freeform .md (old append-only format) must not be lost.
    data = tmp_path / "data"
    data.mkdir()
    (data / "needs_human.md").write_text("## legacy entry\n- old freeform note\n", encoding="utf-8")
    af.append_needs_human(tmp_path, failure=make_failure(nodeid="t::new"), action=None, reason="r")
    archive = data / "needs_human.archive.md"
    assert archive.exists()
    assert "legacy entry" in archive.read_text(encoding="utf-8")
    # The live .md is now the rendered open view.
    assert "t::new" in (data / "needs_human.md").read_text(encoding="utf-8")
