"""bundle_failure — imported-symbol definition bundling (tier-2 context fix).

Regression for the dogfood finding: for an AssertionError the traceback only
names the test, so the implementation under test was never bundled and Claude
couldn't fix logic bugs. bundle_failure now also includes the file that
DEFINES the test's imported symbols (following re-exports)."""
from __future__ import annotations

from orchestrator.harness import failure as fmod
from orchestrator.harness.failure import bundle_failure
from conftest import make_failure


def test_imported_symbols_parses_forms():
    src = (
        "from pkg.a import foo\n"
        "from pkg.b import bar, baz as qux\n"
        "from pkg.c import (one,\n    two)\n"
        "import os\n"
        "from pkg.d import *\n"
    )
    syms = fmod._imported_symbols(src)
    assert {"foo", "bar", "baz", "one", "two"} <= syms
    assert "qux" not in syms   # alias -> original name kept
    assert "*" not in syms


def _mk_repo(tmp_path):
    """A repo with src/pkg/impl.py defining a symbol, re-exported by api.py,
    and a test that imports it from the re-export module."""
    src = tmp_path / "src" / "pkg"
    src.mkdir(parents=True)
    (src / "__init__.py").write_text("", encoding="utf-8")
    (src / "impl.py").write_text(
        "def verdict(x):\n    return 'kept' if x > 0 else 'reverted'\n", encoding="utf-8")
    (src / "api.py").write_text("from .impl import verdict\n", encoding="utf-8")
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_v.py").write_text(
        "from pkg.api import verdict\n\ndef test_v():\n    assert verdict(1) == 'kept'\n",
        encoding="utf-8")
    return tmp_path


def test_find_definition_files_follows_reexport(tmp_path):
    repo = _mk_repo(tmp_path)
    files = fmod._find_definition_files({"verdict"}, repo, limit=5)
    names = {p.name for p in files}
    # The DEFINITION lives in impl.py (api.py only re-exports it).
    assert "impl.py" in names


def test_bundle_includes_implementation_for_assertion_failure(tmp_path):
    repo = _mk_repo(tmp_path)
    # AssertionError traceback names ONLY the test file (the impl returns
    # normally), exactly the case that used to leave Claude blind.
    fail = make_failure(
        nodeid="tests/test_v.py::test_v",
        file="tests/test_v.py",
        failure_type="failure",
        message="AssertionError: assert 'reverted' == 'kept'",
        traceback='File "tests/test_v.py", line 4, in test_v\n    assert verdict(1) == \'kept\'\nAssertionError',
    )
    bundle = bundle_failure(fail, repo)
    bundled = " ".join(bundle.related_sources.keys())
    assert "impl.py" in bundled, f"impl not bundled; got {list(bundle.related_sources)}"
    # The actual buggy implementation text is now in the prompt for the fixer.
    assert "def verdict" in bundle.prompt


def test_bundle_excludes_the_test_file_itself(tmp_path):
    repo = _mk_repo(tmp_path)
    fail = make_failure(nodeid="tests/test_v.py::test_v", file="tests/test_v.py",
                        traceback='File "tests/test_v.py", line 4\nAssertionError')
    bundle = bundle_failure(fail, repo)
    # test source is its own section; it must not also appear as a related source
    assert all("test_v.py" not in k for k in bundle.related_sources)


# --- failing-test-function extraction (huge test file) ---------------------

def test_enclosing_block_extracts_the_function():
    src = (
        "import os\n"
        "\n"
        "def first():\n"
        "    return 1\n"
        "\n"
        "def target():\n"
        "    x = 5\n"
        "    assert x == 6\n"
        "\n"
        "def after():\n"
        "    return 2\n"
    )
    block = fmod._enclosing_block(src, line=8, max_chars=10_000)  # line inside target()
    assert "def target():" in block
    assert "assert x == 6" in block
    assert "def first" not in block and "def after" not in block


def test_bundle_shows_failing_function_in_large_file(tmp_path):
    """A failing test deep in a huge file must still be shown (not lost to a
    head-truncation), with a terse traceback that doesn't name the symbol."""
    src = tmp_path / "src" / "pkg"
    src.mkdir(parents=True)
    (src / "__init__.py").write_text("", encoding="utf-8")
    (src / "impl.py").write_text("def verdict(x):\n    return 'kept' if x > 0 else 'reverted'\n",
                                 encoding="utf-8")
    tests = tmp_path / "tests"
    tests.mkdir()
    # 600 lines of filler, then the real failing test importing the symbol locally.
    filler = "".join(f"# filler line {i}\n" for i in range(600))
    body = (
        "def test_deep():\n"
        "    from pkg.impl import verdict\n"
        "    assert verdict(1) == 'kept'\n"
    )
    (tests / "test_big.py").write_text(filler + body, encoding="utf-8")
    fail_line = filler.count("\n") + 3  # the assert line

    fail = make_failure(
        nodeid="tests/test_big.py::test_deep", file="tests/test_big.py",
        line=fail_line, failure_type="failure",
        message="AssertionError",
        traceback="tests/test_big.py:%d: AssertionError" % fail_line,  # terse
    )
    bundle = bundle_failure(fail, tmp_path, test_source_chars=2000)
    # The failing function is shown despite being far past the 2000-char head.
    assert "def test_deep" in bundle.test_source
    assert "verdict(1) == 'kept'" in bundle.test_source
    # And its implementation got bundled.
    assert any("impl.py" in k for k in bundle.related_sources)
