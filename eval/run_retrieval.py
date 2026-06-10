from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--golden", default="data/golden_set.json")
    args = parser.parse_args()

    golden = json.loads(Path(args.golden).read_text(encoding="utf-8"))
    if not golden:
        print({"recall_at_5": None, "message": "golden_set is empty"})
        return

    raise NotImplementedError("Retrieval eval requires indexed KB and expected_chunks")


if __name__ == "__main__":
    main()
