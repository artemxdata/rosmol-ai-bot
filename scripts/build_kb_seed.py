from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))

from scripts.index_kb import validate_seed_items
from src.kb.source_extractors import build_seed_from_sources

DEFAULT_XLSX = Path("Новый бот Росмол .xlsx")
DEFAULT_DOCX = [
    Path.home() / "Downloads" / "Форум «Российский Север» интенты.docx",
    Path.home() / "Downloads" / "Фестиваль «Больше, чем путешествие» Интенты .docx",
]


def build_kb_seed(
    xlsx_path: Path,
    docx_paths: list[Path],
    output_path: Path,
    forums_output_path: Path | None,
    extraction_date: date,
) -> None:
    missing = [path for path in [xlsx_path, *docx_paths] if not path.exists()]
    if missing:
        formatted = "\n".join(str(path) for path in missing)
        raise FileNotFoundError(f"KB source file not found:\n{formatted}")

    records, forum_registry = build_seed_from_sources(xlsx_path, docx_paths, extraction_date)
    validate_seed_items(records)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(records, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    if forums_output_path:
        forums_output_path.parent.mkdir(parents=True, exist_ok=True)
        forums_output_path.write_text(
            json.dumps(forum_registry, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    print(
        "kb_seed_built "
        f"records={len(records)} "
        f"forums={len(forum_registry)} "
        f"out={output_path}"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build data/knowledge_base_seed.json from local Excel and DOCX sources."
    )
    parser.add_argument("--xlsx", type=Path, default=DEFAULT_XLSX)
    parser.add_argument("--docx", type=Path, action="append", default=None)
    parser.add_argument("--out", type=Path, default=Path("data/knowledge_base_seed.json"))
    parser.add_argument("--forums-out", type=Path, default=Path("data/forums_registry.json"))
    parser.add_argument("--no-forums-out", action="store_true")
    parser.add_argument(
        "--extraction-date",
        type=date.fromisoformat,
        default=date.today(),
        help="Extraction date in YYYY-MM-DD format.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    docx_paths = args.docx if args.docx is not None else DEFAULT_DOCX
    build_kb_seed(
        xlsx_path=args.xlsx,
        docx_paths=docx_paths,
        output_path=args.out,
        forums_output_path=None if args.no_forums_out else args.forums_out,
        extraction_date=args.extraction_date,
    )
