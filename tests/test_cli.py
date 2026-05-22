"""CLI wiring — argument parsing + the `report` command end-to-end."""
from __future__ import annotations

import json

from orchestrator.cli import build_parser


def test_parser_wires_report_command():
    args = build_parser().parse_args(["report"])
    assert args.command == "report"
    assert args.func.__name__ == "_cmd_report"
    assert args.json is False


def test_parser_report_json_flag():
    args = build_parser().parse_args(["--project-root", "/x", "report", "--json"])
    assert args.json is True
    assert args.project_root == "/x"


def test_parser_status_still_wired():
    args = build_parser().parse_args(["status"])
    assert args.func.__name__ == "_cmd_status"


def test_cmd_report_runs_end_to_end(tmp_path, capsys):
    data = tmp_path / "data"
    data.mkdir()
    (data / "events.jsonl").write_text(
        json.dumps({"event": "auto_fix_attempt", "status": "fixed",
                    "claude_retry_used": False}) + "\n", encoding="utf-8")

    args = build_parser().parse_args(["--project-root", str(tmp_path), "report"])
    rc = args.func(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "Orchestrator activity report" in out


def test_cmd_report_json_output_parses(tmp_path, capsys):
    (tmp_path / "data").mkdir()
    args = build_parser().parse_args(["--project-root", str(tmp_path), "report", "--json"])
    rc = args.func(args)
    assert rc == 0
    parsed = json.loads(capsys.readouterr().out)
    assert "fixes" in parsed and "tiers" in parsed
