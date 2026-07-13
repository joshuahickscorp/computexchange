#!/usr/bin/env python3
"""Strictly replay a temporal-prediction frontier receipt and artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
from typing import Sequence


HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import screen_temporal_prediction_frontier as frontier  # noqa: E402


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("receipt", type=Path)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _parse_args(argv)
        receipt = frontier.validate_receipt_path(args.receipt)
        data = args.receipt.read_bytes()
        print(
            json.dumps(
                {
                    "kind": receipt["kind"],
                    "ok": True,
                    "receipt": str(args.receipt),
                    "receipt_sha256": hashlib.sha256(data).hexdigest(),
                    "schema_version": receipt["schema_version"],
                },
                sort_keys=True,
            )
        )
        return 0
    except BaseException as exc:
        print(
            json.dumps(
                {
                    "error": f"{type(exc).__name__}: {exc}"[:4000],
                    "kind": frontier.KIND,
                    "ok": False,
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
