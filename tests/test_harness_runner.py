"""runner.parse_junit_xml — file/line recovery from traceback (dogfood fix).

pytest's JUnit XML sometimes omits the testcase file/line (notably with
dotted classnames). The parser recovers them from the traceback so the
failure bundler can locate + extract the failing test."""
from __future__ import annotations

from orchestrator.harness import runner as rn
from orchestrator.harness.runner import parse_junit_xml


def test_location_from_traceback_prefers_hint_basename():
    tb = ("src/commander_builder/_proposer_sim.py:57: in _verdict_from_ab\n"
          "tests/test_proposer_auto.py:2253: in test_v\n    assert x == y\nAssertionError")
    loc = rn._location_from_traceback(tb, "tests/test_proposer_auto.py")
    assert loc == ("tests/test_proposer_auto.py", 2253)


def test_location_from_traceback_falls_back_to_deepest():
    tb = "a/b.py:10: in f\n c/d.py:20: in g\nBoom"
    assert rn._location_from_traceback(tb, None) == ("c/d.py", 20)


def test_location_from_traceback_none_when_no_frame():
    assert rn._location_from_traceback("no file here", "x.py") is None


def _write_junit(path, *, file_attr, classname, traceback):
    fa = f' file="{file_attr}"' if file_attr else ""
    path.write_text(
        '<?xml version="1.0"?>\n'
        '<testsuites><testsuite tests="1" failures="1" errors="0" skipped="0">\n'
        f'<testcase classname="{classname}" name="test_v"{fa}>\n'
        f'<failure message="AssertionError">{traceback}</failure>\n'
        "</testcase></testsuite></testsuites>\n",
        encoding="utf-8",
    )


def test_parse_recovers_file_and_line_when_omitted(tmp_path):
    xml = tmp_path / "r.xml"
    _write_junit(
        xml, file_attr="", classname="tests.test_proposer_auto",
        traceback="tests/test_proposer_auto.py:2253: in test_v\n    assert a == b\nAssertionError",
    )
    _counts, failures = parse_junit_xml(xml)
    assert len(failures) == 1
    f = failures[0]
    # file recovered from the matching traceback frame; line too.
    assert f.file == "tests/test_proposer_auto.py"
    assert f.line == 2253


def test_parse_keeps_explicit_file_attr(tmp_path):
    xml = tmp_path / "r.xml"
    _write_junit(
        xml, file_attr="tests/test_x.py", classname="tests.test_x",
        traceback="tests/test_x.py:9: in test_v\nAssertionError",
    )
    _counts, failures = parse_junit_xml(xml)
    assert failures[0].file == "tests/test_x.py"
    assert failures[0].line == 9
