#!/usr/bin/env python3
"""Fuse an experiment's README, source data (CSVs), and plotting code into its PNGs.

Each figure becomes *self-contained*: the README that explains it, the exact CSV
data behind it, and the code that produced it all travel inside the PNG as
compressed text chunks (PNG ``zTXt``). The CSVs are still written to disk as
normal — this only adds an embedded copy so a stray ``.png`` is never orphaned
from its context.

Embedded entries use Latin-1 keywords namespaced under ``sidecar:``:

    sidecar:readme              -> README.md text
    sidecar:code/<file>         -> a plotting source file
    sidecar:data/<file.csv>     -> a CSV produced by the experiment
    sidecar:manifest            -> JSON index of everything embedded

Library use (the standard final step in a driver)::

    import png_sidecar
    png_sidecar.make_self_contained(out_dir, code_files=[__file__],
                                    readme_path=EXP_DIR / "README.md")

CLI use::

    python png_sidecar.py list    figure.png
    python png_sidecar.py extract figure.png -o ./unpacked
    python png_sidecar.py show    figure.png sidecar:readme
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Iterable

from PIL import Image, PngImagePlugin

PREFIX = "sidecar:"
MAX_KEYWORD = 79  # PNG tEXt/zTXt keyword length limit


def _safe_keyword(raw: str) -> str:
    """Clamp a keyword to the PNG limit, keeping the tail (filename) readable."""
    if len(raw) <= MAX_KEYWORD:
        return raw
    return raw[: MAX_KEYWORD - 1] + "~"


def embed_into_png(
    png_path: Path, texts: dict[str, str], *, compress: bool = True
) -> None:
    """Add ``texts`` to ``png_path`` as PNG text chunks, in place.

    Existing text chunks and the image DPI are preserved; keys already present
    are overwritten. Values are stored compressed (``zTXt``) by default.
    """
    png_path = Path(png_path)
    with Image.open(png_path) as img:
        img.load()
        existing = dict(getattr(img, "text", {}) or {})
        dpi = img.info.get("dpi")
        pnginfo = PngImagePlugin.PngInfo()
        merged = {**existing, **texts}
        for key, value in merged.items():
            keyword = _safe_keyword(str(key))
            pnginfo.add_text(keyword, str(value), zip=compress)
        save_kwargs = {"pnginfo": pnginfo}
        if dpi:
            save_kwargs["dpi"] = dpi
        img.save(png_path, format="PNG", **save_kwargs)


def util_code_files() -> list[Path]:
    """Return the shared util library — every ``.py`` in this directory.

    Drivers pass ``code_files=[Path(__file__), *util_code_files()]`` so each PNG
    embeds the experiment's own (payload-bearing) script plus every shared module it
    builds on. This keeps figures fully self-contained now that the former monolithic
    ``trace_stats_core.py`` is split across cohesive modules.
    """
    here = Path(__file__).resolve().parent
    return sorted(here.glob("*.py"))


def make_self_contained(
    output_dir: Path,
    *,
    code_files: Iterable[Path] | None = None,
    readme_path: Path | None = None,
    png_names: Iterable[str] | None = None,
    data_glob: str = "*.csv",
    extra: dict[str, str] | None = None,
    compress: bool = True,
) -> list[Path]:
    """Embed README + code + CSV data into every PNG under ``output_dir``.

    Returns the list of PNG paths updated. Safe to call when there are no PNGs
    (e.g. CSV/text-only experiments): it simply does nothing.
    """
    output_dir = Path(output_dir)
    texts: dict[str, str] = {}
    manifest: dict[str, object] = {"entries": []}

    if readme_path is not None and Path(readme_path).exists():
        body = Path(readme_path).read_text(encoding="utf-8")
        texts[f"{PREFIX}readme"] = body
        manifest["entries"].append({"key": f"{PREFIX}readme", "chars": len(body)})

    for code_file in code_files or []:
        code_file = Path(code_file)
        if not code_file.exists():
            continue
        body = code_file.read_text(encoding="utf-8")
        key = f"{PREFIX}code/{code_file.name}"
        texts[key] = body
        manifest["entries"].append({"key": key, "chars": len(body)})

    for csv_path in sorted(output_dir.glob(data_glob)):
        body = csv_path.read_text(encoding="utf-8")
        key = f"{PREFIX}data/{csv_path.name}"
        texts[key] = body
        manifest["entries"].append({"key": key, "chars": len(body)})

    for key, value in (extra or {}).items():
        full = key if key.startswith(PREFIX) else f"{PREFIX}{key}"
        texts[full] = value
        manifest["entries"].append({"key": full, "chars": len(value)})

    pngs = (
        [output_dir / name for name in png_names]
        if png_names is not None
        else sorted(output_dir.glob("*.png"))
    )
    if not pngs:
        return []

    texts[f"{PREFIX}manifest"] = json.dumps(manifest, indent=2)
    updated: list[Path] = []
    for png in pngs:
        if not png.exists():
            continue
        embed_into_png(png, texts, compress=compress)
        updated.append(png)

    print(
        f"Embedded README/code/data into {len(updated)} PNG(s) under {output_dir}",
        file=sys.stderr,
    )
    return updated


def extract_from_png(png_path: Path, *, only_sidecar: bool = True) -> dict[str, str]:
    """Return embedded text entries from a PNG (decompressed)."""
    with Image.open(png_path) as img:
        img.load()
        text = dict(getattr(img, "text", {}) or {})
    if only_sidecar:
        return {k: v for k, v in text.items() if k.startswith(PREFIX)}
    return text


# --------------------------------------------------------------------------- CLI


def _cmd_list(args: argparse.Namespace) -> int:
    entries = extract_from_png(args.png, only_sidecar=not args.all)
    if not entries:
        print("(no embedded sidecar entries)", file=sys.stderr)
        return 1
    for key in sorted(entries):
        print(f"{len(entries[key]):>9,d}  {key}")
    return 0


def _cmd_show(args: argparse.Namespace) -> int:
    entries = extract_from_png(args.png, only_sidecar=False)
    if args.key not in entries:
        print(f"key not found: {args.key}", file=sys.stderr)
        return 1
    sys.stdout.write(entries[args.key])
    if not entries[args.key].endswith("\n"):
        sys.stdout.write("\n")
    return 0


def _cmd_extract(args: argparse.Namespace) -> int:
    entries = extract_from_png(args.png, only_sidecar=True)
    if not entries:
        print("(no embedded sidecar entries)", file=sys.stderr)
        return 1
    out_dir = args.out or Path(f"extracted_{Path(args.png).stem}")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    for key, value in entries.items():
        rel = key[len(PREFIX) :]
        if rel == "readme":
            rel = "README.md"
        elif rel == "manifest":
            rel = "manifest.json"
        dest = out_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(value, encoding="utf-8")
        print(f"wrote {dest}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="List embedded entries and sizes")
    p_list.add_argument("png", type=Path)
    p_list.add_argument(
        "--all", action="store_true", help="Include non-sidecar text chunks"
    )
    p_list.set_defaults(func=_cmd_list)

    p_show = sub.add_parser("show", help="Print one embedded entry to stdout")
    p_show.add_argument("png", type=Path)
    p_show.add_argument("key", help="Entry key, e.g. sidecar:readme")
    p_show.set_defaults(func=_cmd_show)

    p_extract = sub.add_parser("extract", help="Unpack embedded entries to files")
    p_extract.add_argument("png", type=Path)
    p_extract.add_argument("-o", "--out", type=Path, default=None)
    p_extract.set_defaults(func=_cmd_extract)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
