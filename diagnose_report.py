"""
Standalone diagnostic for "No sections found" upload errors.

Run this directly against a SystemPerformance/pButtons HTML file that fails to
parse. It never prints anything from inside a <pre>...</pre> block (where the
actual sensitive report data lives) -- only the HTML tag scaffolding around
section boundaries, capped to a short length. That output is safe to paste
into a bug report or share with the maintainer.

Usage:
    python3 diagnose_report.py path/to/report.html
    python3 diagnose_report.py path/to/report.html --max 10 --max-len 300

If sysperfsight_parser.py is importable (i.e. this script sits in the repo
next to it), the real parse_sections() is run first so the report matches
exactly what the app would do. If it's not importable (e.g. this script was
copied out on its own), diagnostics still run using local copies of the same
probes.
"""
import argparse
import re
import sys


def _safe_snippet(html: str, pos: int, context: int, max_len: int) -> str:
    """Return a bounded excerpt starting near `pos`, cut off before any <pre> content."""
    start = max(0, pos - context)
    pre_pos = html.find("<pre", pos)
    hard_cap = pos + max_len
    end = min(pre_pos, hard_cap) if pre_pos != -1 else hard_cap
    end = max(end, start)
    return html[start:end]


def diagnose(path: str, max_hits: int, context: int, max_len: int) -> None:
    with open(path, "rb") as f:
        raw = f.read()
    html = raw.decode("iso-8859-1", errors="replace")
    print(f"File: {path} ({len(raw)} bytes)\n")

    try:
        from sysperfsight_parser import parse_sections
        _, sections = parse_sections(html)
        print(f"parse_sections() found {len(sections)} section(s).")
        if sections:
            print("Titles/IDs: " + ", ".join(f"{s.title!r}({s.id})" for s in sections))
            print("\nParsing succeeded -- no diagnostics needed.")
            return
        print()
    except ImportError:
        print("(sysperfsight_parser not importable from here -- running raw probes only.)\n")

    noshade_hits = [m.start() for m in re.finditer(r"noshade", html, re.IGNORECASE)]
    div_hits = [m.start() for m in re.finditer(r"<div\s+id=", html, re.IGNORECASE)]
    print(f"'noshade' occurrences: {len(noshade_hits)}")
    print(f"'<div id=' occurrences: {len(div_hits)}")
    print()

    if not noshade_hits and not div_hits:
        print("Neither marker appears anywhere in the file -- this likely isn't an")
        print("unmodified SystemPerformance/pButtons HTML export (wrong file, or it")
        print("was re-saved/reformatted by another tool).")
        return

    def dump(label: str, hits: list[int]) -> None:
        if not hits:
            return
        print(f"--- {label} (first {min(max_hits, len(hits))} of {len(hits)}) ---")
        for i, pos in enumerate(hits[:max_hits]):
            snippet = _safe_snippet(html, pos, context, max_len)
            print(f"[{label} #{i} @ offset {pos}] {snippet!r}")
        print()

    dump("noshade", noshade_hits)
    dump("div-id", div_hits)

    print(
        "Eyeball the excerpts above before sharing: they should only contain HTML\n"
        "tags/attributes and short section titles (e.g. 'mgstat', 'sar -d'), never\n"
        "hostnames, paths, or credentials. Redact anything that looks otherwise."
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("report", help="Path to the SystemPerformance/pButtons .html file")
    parser.add_argument("--max", type=int, default=6, help="Max occurrences to print per marker (default 6)")
    parser.add_argument("--context", type=int, default=30, help="Bytes of context before each hit (default 30)")
    parser.add_argument("--max-len", type=int, default=400, help="Max excerpt length in bytes (default 400)")
    args = parser.parse_args()

    try:
        diagnose(args.report, args.max, args.context, args.max_len)
    except FileNotFoundError:
        print(f"File not found: {args.report}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
