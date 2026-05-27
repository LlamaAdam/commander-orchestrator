"""Bundle a pytest failure into a model-ready prompt.

`bundle_failure(failure, repo_dir)` returns a `FailureBundle`:
  - the original TestFailure
  - the test source (truncated)
  - up to N related source files referenced in the traceback (truncated)
  - a final assembled `prompt` string ready to feed to the router

The bundler does NOT call any model. It just shapes the prompt. The
orchestrator's router decides whether the resulting prompt should go to the
local model (small/structural failures) or Claude (subtle/architectural ones).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .runner import TestFailure


# Files at/above this size are too big to safely regenerate wholesale with
# replace_file -- the model can corrupt code outside the fix. The prompt
# steers such files to apply_diff (a minimal patch, no blast radius) instead.
LARGE_FILE_CHARS = 8000


@dataclass
class FailureBundle:
    """A self-contained failure context ready to feed to a model."""

    failure: TestFailure
    test_source: str = ""
    related_sources: dict[str, str] = field(default_factory=dict)
    # Original (un-truncated) char count per related source, so the prompt can
    # flag large files and the fixer can prefer apply_diff over replace_file.
    related_source_sizes: dict[str, int] = field(default_factory=dict)
    prompt: str = ""


def _read_truncated(path: Path, max_chars: int) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except (OSError, UnicodeError):
        return ""
    if len(text) > max_chars:
        omitted = len(text) - max_chars
        return text[:max_chars] + f"\n\n# ... truncated ({omitted} more chars) ...\n"
    return text


def _enclosing_block(full_text: str, line: int, max_chars: int) -> str:
    """Return the source of the def/class block containing 1-based `line`.

    Walks up from `line` to the nearest ``def``/``async def``/``class`` header
    (at any indent), then down until the indent returns to that header's level.
    This is how we hand a logic-bug fixer the ACTUAL failing test function in a
    huge test file, instead of a blind head-truncation that misses it."""
    lines = full_text.splitlines()
    if not lines or not (1 <= line <= len(lines)):
        return ""
    start = None
    header_indent = 0
    for i in range(line - 1, -1, -1):
        stripped = lines[i].lstrip()
        if stripped.startswith(("def ", "async def ", "class ")):
            start = i
            header_indent = len(lines[i]) - len(stripped)
            break
    if start is None:
        return ""
    end = len(lines)
    for j in range(start + 1, len(lines)):
        s = lines[j]
        if not s.strip():
            continue
        if (len(s) - len(s.lstrip())) <= header_indent:
            end = j
            break
    block = "\n".join(lines[start:end])
    if len(block) > max_chars:
        block = block[:max_chars] + "\n# ... (function truncated) ...\n"
    return block


# Two patterns cover the common traceback shapes:
#   - `File "path/to/x.py", line 42`  -- standard Python tracebacks
#   - `path/to/x.py:42`               -- pytest --tb=short, log lines
_TRACEBACK_FILE_RES = [
    re.compile(r'File "([^"\n]+\.py)", line (\d+)'),
    re.compile(
        r'(?<![A-Za-z0-9_])'
        r'([A-Za-z]:[\\/][^\s:"\x27`<>|*?]+\.py'
        r'|[./\\][^\s:"\x27`<>|*?]*\.py'
        r'|[A-Za-z0-9_./\\-]+\.py)'
        r':(\d+)'
    ),
]


def _extract_referenced_files(traceback: str, repo_dir: Path) -> list:
    """Find .py files in the traceback that exist inside repo_dir."""
    seen = []
    seen_set = set()
    try:
        repo_resolved = repo_dir.resolve()
    except (OSError, ValueError):
        return []

    matches = []
    for rx in _TRACEBACK_FILE_RES:
        for m in rx.finditer(traceback or ""):
            matches.append((m.start(), m.group(1)))
    matches.sort(key=lambda t: t[0])

    for _pos, path_str in matches:
        candidates = []
        try:
            candidates.append((repo_dir / path_str).resolve())
        except (OSError, ValueError):
            pass
        try:
            candidates.append(Path(path_str).resolve())
        except (OSError, ValueError):
            pass

        for cand in candidates:
            if cand in seen_set:
                break
            try:
                in_repo = (cand == repo_resolved) or (repo_resolved in cand.parents)
                if in_repo and cand.exists() and cand.is_file():
                    seen.append(cand)
                    seen_set.add(cand)
                    break
            except (OSError, ValueError):
                continue
    return seen


# Identifiers a test imports from any module: `from X import a, b as c, (d, e)`.
_FROM_IMPORT_RE = re.compile(r"^\s*from\s+[\w.]+\s+import\s+(.+?)(?:#.*)?$", re.MULTILINE)
_FROM_IMPORT_PAREN_RE = re.compile(r"from\s+[\w.]+\s+import\s+\(([^)]*)\)", re.DOTALL)
# Directories never worth scanning for symbol definitions.
_SKIP_DIRS = {".git", "__pycache__", ".venv", "venv", "node_modules",
              "vendor", "build", "dist", ".pytest_cache", ".mypy_cache", ".tox"}


def _imported_symbols(test_source: str) -> set:
    """Extract symbol names a test imports via `from ... import ...`.

    Handles single-line, comma lists, parenthesized multi-line, and `as`
    aliases (keeps the ORIGINAL name, which is what's defined in source).
    Skips `*`. These are the symbols whose DEFINITIONS we want to bundle so
    a logic-bug fixer can see the implementation, not just the test."""
    out: set = set()
    chunks = _FROM_IMPORT_RE.findall(test_source or "")
    chunks += _FROM_IMPORT_PAREN_RE.findall(test_source or "")
    for chunk in chunks:
        for piece in chunk.replace("(", " ").replace(")", " ").split(","):
            name = piece.strip().split(" as ")[0].strip()
            if name and name != "*" and name.isidentifier():
                out.add(name)
    return out


_DEF_NAME_RE = re.compile(r"^\s*(?:def|class)\s+(\w+)", re.MULTILINE)
_ASSIGN_NAME_RE = re.compile(r"^(\w+)\s*[:=]", re.MULTILINE)


def _find_definition_files(symbols, repo_dir: Path, limit: int) -> list:
    """Find repo source files DEFINING any of `symbols`, in symbol priority order.

    `symbols` is an ORDERED iterable (highest priority first) -- the caller
    puts the symbols that appear in the failing traceback first, so for a huge
    test file the implementation of the *failing* symbol is bundled before
    incidental imports crowd out the budget. Follows re-exports: a symbol only
    re-exported by the imported module still resolves to the file that `def`s
    it. Bounded: skips heavy dirs + test files; returns at most `limit` files."""
    symbols = [s for s in dict.fromkeys(symbols) if s]  # dedupe, keep order
    if not symbols or limit <= 0:
        return []
    try:
        repo_resolved = repo_dir.resolve()
    except (OSError, ValueError):
        return []
    scan_root = repo_resolved / "src" if (repo_resolved / "src").is_dir() else repo_resolved

    # One pass: map each scanned file -> the set of names it defines.
    file_defs: list = []  # (path, set_of_names)
    for py in scan_root.rglob("*.py"):
        if any(part in _SKIP_DIRS for part in py.parts):
            continue
        if py.name.startswith("test_") or py.parent.name == "tests":
            continue  # we want the implementation, not other tests
        try:
            text = py.read_text(encoding="utf-8", errors="replace")
        except (OSError, UnicodeError):
            continue
        names = set(_DEF_NAME_RE.findall(text)) | set(_ASSIGN_NAME_RE.findall(text))
        if names:
            file_defs.append((py, names))

    # Select files in symbol-priority order.
    found: list = []
    found_set = set()
    for sym in symbols:
        for py, names in file_defs:
            if sym in names and py not in found_set:
                found.append(py)
                found_set.add(py)
                if len(found) >= limit:
                    return found
    return found


_FROM_MODULE_RE = re.compile(r"^\s*from\s+([\w.]+)\s+import\s+(.+?)(?:#.*)?$", re.MULTILINE)
_IMPORT_MODULE_RE = re.compile(r"^\s*import\s+([\w.][\w.\s,]*?)(?:\s+as\s+\w+)?(?:#.*)?$", re.MULTILINE)


def _imported_module_paths(test_source: str) -> list:
    """Ordered dotted module paths a test imports, most-specific first.

    Captures `from A.B import x, y` (-> submodule candidates A.B.x / A.B.y,
    then A.B) and `import A.B [as c]`. Submodule candidates rank first so
    `from pkg import submod` resolves to pkg/submod.py, not pkg/__init__.py."""
    submods: list = []
    from_mods: list = []
    import_mods: list = []
    for m in _FROM_MODULE_RE.finditer(test_source or ""):
        mod = m.group(1)
        from_mods.append(mod)
        for piece in m.group(2).replace("(", " ").replace(")", " ").split(","):
            name = piece.strip().split(" as ")[0].strip()
            if name and name != "*" and name.isidentifier():
                submods.append(f"{mod}.{name}")
    for m in _IMPORT_MODULE_RE.finditer(test_source or ""):
        for chunk in m.group(1).split(","):
            mod = chunk.strip().split(" as ")[0].strip()
            if mod and all(p.isidentifier() for p in mod.split(".")):
                import_mods.append(mod)
    return list(dict.fromkeys(submods + from_mods + import_mods))


def _resolve_module_files(test_source: str, repo_dir: Path, limit: int) -> list:
    """Resolve a test's imported modules to repo source files, by PATH.

    Unlike `_find_definition_files` (which needs the symbol already DEFINED),
    this finds the file a test targets even when the function under test does
    not exist yet (TDD work items) or is reached via ``module.attr`` -- the
    cases that otherwise leave the fixer with no source to edit."""
    if limit <= 0:
        return []
    try:
        repo_resolved = repo_dir.resolve()
    except (OSError, ValueError):
        return []
    roots = [r for r in (repo_resolved / "src", repo_resolved) if r.is_dir()]
    found: list = []
    found_set = set()
    for dotted in _imported_module_paths(test_source):
        parts = dotted.split(".")
        hit = None
        for root in roots:
            for rel in (Path(*parts).with_suffix(".py"), Path(*parts) / "__init__.py"):
                cand = root / rel
                if cand.is_file():
                    hit = cand.resolve()
                    break
            if hit:
                break
        # Fallback: a bare module not on a package path (e.g. scripts/foo.py
        # placed on sys.path by the test) -> search by leaf filename.
        if hit is None and len(parts) == 1:
            for root in roots:
                for cand in root.rglob(parts[0] + ".py"):
                    if any(p in _SKIP_DIRS for p in cand.parts):
                        continue
                    hit = cand.resolve()
                    break
                if hit:
                    break
        if hit is None or hit in found_set:
            continue
        if hit.name.startswith("test_") or hit.parent.name == "tests":
            continue
        found.append(hit)
        found_set.add(hit)
        if len(found) >= limit:
            break
    return found


def bundle_failure(
    failure: TestFailure,
    repo_dir: Path,
    *,
    include_related: bool = True,
    max_related_files: int = 3,
    max_definition_files: int = 2,
    test_source_chars: int = 6000,
    related_source_chars: int = 3000,
) -> FailureBundle:
    """Assemble a FailureBundle from a TestFailure + the repo it lives in.

    Related sources come from two places:
      1. files named in the traceback (existing behavior), and
      2. files that DEFINE the symbols the test imports (new) -- crucial for
         assertion failures, whose traceback only names the test, leaving a
         logic-bug fixer blind to the implementation. (2) follows re-exports.
    """
    repo_dir = Path(repo_dir)
    bundle = FailureBundle(failure=failure)

    full_test_text = ""  # untruncated, used only for import parsing
    if failure.file:
        for cand in [repo_dir / failure.file, Path(failure.file)]:
            try:
                resolved = cand.resolve()
            except (OSError, ValueError):
                continue
            if resolved.exists() and resolved.is_file():
                try:
                    full_test_text = resolved.read_text(encoding="utf-8", errors="replace")
                except (OSError, UnicodeError):
                    full_test_text = ""
                # Focus on the FAILING test function (huge test files would
                # otherwise head-truncate it away). Prepend a small import
                # header for context; fall back to head-truncation when the
                # line/block is unavailable.
                block = _enclosing_block(full_test_text, failure.line or 0, test_source_chars) \
                    if full_test_text else ""
                if block and block not in full_test_text[:test_source_chars]:
                    header = full_test_text[:1500]
                    bundle.test_source = (
                        header.rstrip()
                        + "\n\n# ... (file continues; showing the failing test) ...\n\n"
                        + block
                    )
                else:
                    bundle.test_source = (
                        _read_truncated(resolved, test_source_chars)
                        if not full_test_text
                        else (full_test_text[:test_source_chars]
                              + ("" if len(full_test_text) <= test_source_chars
                                 else "\n\n# ... truncated ...\n")))
                break

    if include_related:
        test_abs = None
        if failure.file:
            try:
                test_abs = (repo_dir / failure.file).resolve()
            except (OSError, ValueError):
                test_abs = None

        related: list = []
        if failure.traceback:
            related = _extract_referenced_files(failure.traceback, repo_dir)
        if test_abs is not None:
            related = [p for p in related if p != test_abs]
        related = related[:max_related_files]

        # Module files the test imports -- the actual edit targets. Resolved
        # by path, so found even when the function under test doesn't exist
        # yet (TDD) or is reached via `module.attr` -- cases neither the
        # traceback nor _find_definition_files can surface. Highest priority,
        # so prepend (reversed -> first import ends up first after inserts).
        for p in reversed(_resolve_module_files(
            full_test_text or bundle.test_source, repo_dir,
            max_related_files + max_definition_files,
        )):
            if (test_abs is None or p != test_abs) and p not in related:
                related.insert(0, p)

        # Add definition files for the test's imported symbols (deduped,
        # excluding the test itself + anything already in `related`).
        # Priority: imported symbols that ALSO appear in the failing
        # traceback come first, so the implementation of the *failing*
        # symbol wins the budget over incidental top-of-file imports.
        imported = _imported_symbols(full_test_text or bundle.test_source)
        # Prioritize symbols that appear in the failing context -- the
        # traceback AND the shown test body (which now includes the failing
        # test function). This makes the implementation of the *failing*
        # symbol win the bundle budget even when the JUnit traceback is terse.
        ctx = (failure.traceback or "") + "\n" + (bundle.test_source or "")
        ctx_idents = set(re.findall(r"[A-Za-z_]\w*", ctx))
        ordered_syms = ([s for s in imported if s in ctx_idents]
                        + [s for s in imported if s not in ctx_idents])
        already = set(related)
        if test_abs is not None:
            already.add(test_abs)
        for p in _find_definition_files(
            ordered_syms, repo_dir, max_definition_files * 3
        ):
            if p not in already:
                related.append(p)
                already.add(p)
            if len(related) >= max_related_files + max_definition_files:
                break

        for p in related:
            try:
                rel_str = str(p.relative_to(repo_dir.resolve()))
            except ValueError:
                rel_str = str(p)
            try:
                full = p.read_text(encoding="utf-8", errors="replace")
            except (OSError, UnicodeError):
                full = ""
            bundle.related_source_sizes[rel_str] = len(full)
            if len(full) > related_source_chars:
                omitted = len(full) - related_source_chars
                bundle.related_sources[rel_str] = (
                    full[:related_source_chars]
                    + f"\n\n# ... truncated ({omitted} more chars) ...\n"
                )
            else:
                bundle.related_sources[rel_str] = full

    bundle.prompt = _assemble_prompt(bundle)
    return bundle


def _assemble_prompt(bundle: FailureBundle) -> str:
    f = bundle.failure
    line_str = str(f.line) if f.line is not None else "?"
    parts = [
        f"A pytest {f.failure_type} occurred. Diagnose the root cause and propose a minimal fix.",
        "",
        f"## Test: `{f.nodeid}`",
        f"- file: `{f.file}` (line {line_str})",
        f"- type: `{f.failure_type}`",
        "",
        "## Pytest message",
        "```",
        (f.message.strip() or "(no message)"),
        "```",
        "",
        "## Traceback",
        "```",
        (f.traceback or "(no traceback)").rstrip(),
        "```",
    ]

    if bundle.test_source:
        parts.extend([
            "",
            f"## Source: `{f.file}`",
            "```python",
            bundle.test_source.rstrip(),
            "```",
        ])

    for rel, src in bundle.related_sources.items():
        size = bundle.related_source_sizes.get(rel, len(src))
        marker = (f" — {size} chars; LARGE: prefer apply_diff over replace_file"
                  if size >= LARGE_FILE_CHARS else f" — {size} chars")
        parts.extend([
            "",
            f"## Related source: `{rel}`{marker}",
            "```python",
            src.rstrip(),
            "```",
        ])

    parts.extend([
        "",
        "## Task",
        ("Explain the root cause in 2-4 sentences, then propose a minimal "
         "fix as a unified diff against the affected file(s). If you need "
         "more code to diagnose confidently, say so and list the exact "
         "file(s) you would want to see."),
    ])
    return "\n".join(parts)
