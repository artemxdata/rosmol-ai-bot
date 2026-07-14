from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.append(str(Path(__file__).resolve().parents[1]))

from src.kb.audit import audit_seed_records


def audit_kb_seed(
    path: Path,
    output_path: Path | None,
    fail_on: str | None,
    *,
    forums_registry_path: Path | None = None,
    min_forum_chunks: int = 0,
    min_forum_topics: int = 0,
    markdown_path: Path | None = None,
) -> dict:
    records = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(records, list):
        raise ValueError("knowledge_base_seed.json must contain a JSON array")

    forum_registry = _read_forum_registry(forums_registry_path)
    report = audit_seed_records(
        records,
        forum_registry=forum_registry,
        min_forum_chunks=min_forum_chunks,
        min_forum_topics=min_forum_topics,
    )
    encoded = json.dumps(report, ensure_ascii=False, indent=2)
    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(encoded + "\n", encoding="utf-8")
    if markdown_path:
        write_markdown(markdown_path, report)
    print(encoded)

    if fail_on == "error" and report["errors"]:
        raise SystemExit(1)
    if fail_on == "warning" and (report["errors"] or report["warnings"]):
        raise SystemExit(1)
    return report


def _read_forum_registry(path: Path | None) -> list[dict]:
    if path is None:
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("forums registry must contain a JSON array")
    return payload


def write_markdown(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    summary = report.get("summary") or {}
    lines = [
        "# KB Seed Audit",
        "",
        f"- Records: `{report.get('records_total')}`",
        f"- Errors: `{report.get('errors')}`",
        f"- Warnings: `{report.get('warnings')}`",
        f"- Forums total: `{summary.get('forums_total')}`",
        f"- Generic records: `{summary.get('generic_records_count')}`",
        "",
        "## Distribution",
        "",
        f"- Categories: `{json.dumps(summary.get('category_counts') or {}, ensure_ascii=False)}`",
        f"- Statuses: `{json.dumps(summary.get('status_counts') or {}, ensure_ascii=False)}`",
        f"- Sources: `{json.dumps(summary.get('source_type_counts') or {}, ensure_ascii=False)}`",
        "",
        "## Findings",
        "",
    ]
    findings = report.get("findings") or []
    if not findings:
        lines.append("- No findings.")
    for finding in findings:
        lines.append(
            f"- `{finding.get('severity')}` `{finding.get('code')}` "
            f"count=`{finding.get('count')}`: {finding.get('message')}"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit generated KB seed quality.")
    parser.add_argument("--path", type=Path, default=Path("data/knowledge_base_seed.json"))
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--markdown", type=Path, default=None)
    parser.add_argument(
        "--forums-registry",
        type=Path,
        default=Path("data/forums_registry.json"),
    )
    parser.add_argument("--min-forum-chunks", type=int, default=0)
    parser.add_argument("--min-forum-topics", type=int, default=0)
    parser.add_argument("--fail-on", choices=["error", "warning"], default=None)
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    audit_kb_seed(
        args.path,
        args.output,
        args.fail_on,
        forums_registry_path=args.forums_registry,
        min_forum_chunks=args.min_forum_chunks,
        min_forum_topics=args.min_forum_topics,
        markdown_path=args.markdown,
    )
