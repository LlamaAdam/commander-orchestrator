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


def test_parse_replace_file_populates_path_and_content():
    fa = af.parse_fix_action(
        '{"action":"replace_file","confidence":0.9,"path":"src/foo.py",'
        '"new_content":"x = 2\\n"}'
    )
    assert fa.action == "replace_file"
    assert fa.path == "src/foo.py"
    assert fa.new_content == "x = 2\n"
    # path is mirrored into files_touched so the gating/commit/revert machinery works.
    assert fa.files_touched == ["src/foo.py"]


def test_parse_replace_file_keeps_explicit_files_touched():
    fa = af.parse_fix_action(
        '{"action":"replace_file","confidence":0.9,"path":"src/foo.py",'
        '"new_content":"x\\n","files_touched":["src/foo.py"]}'
    )
    assert fa.files_touched == ["src/foo.py"]  # not duplicated


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
