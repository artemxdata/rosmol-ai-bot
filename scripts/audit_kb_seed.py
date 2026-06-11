from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))

from src.kb.audit import audit_seed_records


def audit_kb_seed(path: Path, output_path: Path | None, fail_on: str | None) -> dict:
    records = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(records, list):
        raise ValueError("knowledge_base_seed.json must contain a JSON array")

    report = audit_seed_records(records)
    encoded = json.dumps(report, ensure_ascii=False, indent=2)
    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)

    if fail_on == "error" and report["errors"]:
        raise SystemExit(1)
    if fail_on == "warning" and (report["errors"] or report["warnings"]):
        raise SystemExit(1)
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit generated KB seed quality.")
    parser.add_argument("--path", type=Path, default=Path("data/knowledge_base_seed.json"))
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--fail-on", choices=["error", "warning"], default=None)
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    audit_kb_seed(args.path, args.output, args.fail_on)
