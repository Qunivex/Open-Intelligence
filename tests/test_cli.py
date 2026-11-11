"""Human-readable CLI output."""

from __future__ import annotations

from typing import Any

from reuse_layer.adapters.cli import _print_find


def test_find_output_shows_pinned_ref_freshness(capsys: Any) -> None:
    _print_find(
        {
            "verdict": "reuse",
            "build_yourself": {"reason": "one project covers this task"},
            "picks": [
                {
                    "id": "pkg:github/acme/project@v1.0.0",
                    "scores": {"total": 0.8},
                    "summary": "Example project",
                    "why": [],
                    "pin": {"ref": "v1.0.0"},
                    "ref_freshness": {
                        "pinned_commit_at": "2026-01-01T00:00:00+00:00",
                        "default_branch": "main",
                        "default_branch_commit_at": "2026-02-01T00:00:00+00:00",
                        "default_branch_is_newer": True,
                        "default_branch_ahead_days": 31.0,
                        "default_branch_ahead_minutes": 44640.0,
                    },
                    "get": "git clone example",
                }
            ],
            "considered": 1,
            "relevance_judge": "lexical",
            "elapsed_ms": 10,
            "recommendation_id": "rec_example",
        }
    )

    output = capsys.readouterr().out
    assert "ref freshness:" in output
    assert "pinned v1.0.0" in output
    assert "main is newer than pinned v1.0.0 by 31 days" in output


def test_find_output_shows_small_freshness_difference_in_minutes(capsys: Any) -> None:
    _print_find(
        {
            "verdict": "reuse",
            "build_yourself": {"reason": "one project covers this task"},
            "picks": [
                {
                    "id": "pkg:github/acme/project@v1.0.0",
                    "scores": {"total": 0.8},
                    "summary": "Example project",
                    "why": [],
                    "pin": {"ref": "v1.0.0"},
                    "ref_freshness": {
                        "pinned_commit_at": "2026-01-01T00:00:00+00:00",
                        "default_branch": "main",
                        "default_branch_commit_at": "2026-01-01T00:23:24+00:00",
                        "default_branch_is_newer": True,
                        "default_branch_ahead_days": 0.016,
                        "default_branch_ahead_minutes": 23.4,
                    },
                    "get": "git clone example",
                }
            ],
            "considered": 1,
            "relevance_judge": "lexical",
            "elapsed_ms": 10,
            "recommendation_id": "rec_example",
        }
    )

    assert "main is newer than pinned v1.0.0 by 23.4 minutes" in capsys.readouterr().out
