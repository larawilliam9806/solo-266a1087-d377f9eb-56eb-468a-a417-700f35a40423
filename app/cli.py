"""Command line interface for local use / ops scripting.

Example:
    python -m app.cli \\
        --layer layer1.tar --layer layer2.tar \\
        --dir /etc/nginx --out nginx.tar --layout basename
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .engine import (
    DirectoryNotFound,
    InvalidLayer,
    NotADirectory,
    merge_layers,
    parse_layout,
    write_directory_tar,
)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="layer-extract",
        description=(
            "Merge uncompressed container image layer tars (bottom to top) "
            "and export the final visible directory as a standalone tar."
        ),
    )
    p.add_argument(
        "--layer",
        "-l",
        action="append",
        default=[],
        help="Layer tar path; repeat in bottom-to-top upload order.",
    )
    p.add_argument("--dir", "-d", required=True, help="Directory to export.")
    p.add_argument("--out", "-o", required=True, help="Output tar path ('-' stdout).")
    p.add_argument(
        "--layout",
        default="basename",
        help="Archive naming: basename (default), relative/contents, full/absolute.",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.layer:
        print("error: at least one --layer is required", file=sys.stderr)
        return 2

    layout = parse_layout(args.layout)
    try:
        model = merge_layers(args.layer)
        if args.out == "-":
            stats = write_directory_tar(model, args.dir, sys.stdout.buffer, layout)
        else:
            with open(args.out, "wb") as fh:
                stats = write_directory_tar(model, args.dir, fh, layout)
    except DirectoryNotFound as exc:
        print(f"not found: {exc.path or '/'}", file=sys.stderr)
        return 3
    except NotADirectory as exc:
        print(f"not a directory: {exc.path}", file=sys.stderr)
        return 4
    except InvalidLayer as exc:
        print(f"invalid layer: {exc}", file=sys.stderr)
        return 5

    print(
        f"wrote {stats['members']} members "
        f"({stats['hardlinks']} hardlinks) layout={stats['layout']}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
