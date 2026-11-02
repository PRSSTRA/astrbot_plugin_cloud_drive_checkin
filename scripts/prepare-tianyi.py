#!/usr/bin/env python3
"""Build the pinned Tianyi zipapp from a local wheelhouse."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPOSITORY_ROOT))

from preparation import PreparationError, build_tianyi


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prepare the pinned Tianyi executable artifact offline."
    )
    parser.add_argument("--wheelhouse", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser


def _write_new(path: Path, payload: bytes) -> None:
    created = False
    try:
        with path.open("xb") as output:
            created = True
            output.write(payload)
    except OSError:
        if created:
            path.unlink(missing_ok=True)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    arguments = parser.parse_args(argv)
    if arguments.output.exists() or arguments.output.is_symlink():
        parser.error("output already exists; refusing to overwrite it")
    try:
        artifact = build_tianyi(arguments.wheelhouse)
        _write_new(arguments.output, artifact.script)
    except PreparationError as exc:
        parser.error(str(exc))
    except OSError:
        parser.error("cannot create the output file")

    sys.stdout.write(
        json.dumps(
            {
                "source_commit": artifact.source_commit,
                "source_sha256": artifact.source_sha256,
                "sha256": artifact.sha256,
                "size": len(artifact.script),
            },
            separators=(",", ":"),
        )
        + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
