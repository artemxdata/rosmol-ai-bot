from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--metrics", default="eval/metrics.json")
    parser.add_argument("--threshold", type=float, default=0.85)
    args = parser.parse_args()

    path = Path(args.metrics)
    if not path.exists():
        print("metrics file is absent; skipping until golden_set is available")
        return
    metrics = json.loads(path.read_text(encoding="utf-8"))
    recall = metrics.get("recall_at_5")
    if recall is not None and recall < args.threshold:
        raise SystemExit(f"Recall@5 regression: {recall:.3f} < {args.threshold:.3f}")


if __name__ == "__main__":
    main()
