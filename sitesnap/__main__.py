"""CLI entry point.

    sitesnap https://example.com                 # full pipeline: discover -> capture -> qa -> report -> PDF
    sitesnap https://example.com --max-pages 50 --viewports desktop mobile --no-pdf
    sitesnap serve                               # local web UI on http://127.0.0.1:8765
    sitesnap --config snapshots/<site>/capture.config.json capture   # resume / advanced stage-by-stage use
"""
from __future__ import annotations

import argparse
import sys

from .common import load_config

STAGE_CMDS = {"discover", "capture", "qa", "integrity", "report", "pdfbook", "all"}


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    # `sitesnap <url>` shorthand -> `sitesnap run <url>`
    if argv and argv[0] not in STAGE_CMDS | {"run", "serve", "-h", "--help", "--config"} and not argv[0].startswith("-"):
        argv.insert(0, "run")

    ap = argparse.ArgumentParser(prog="sitesnap", description="Paste a URL, get full-site screenshots (desktop/tablet/mobile), HTML, headers and a PDF report.")
    ap.add_argument("--config", default=None, help="capture.config.json of an existing snapshot (stage commands)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("run", help="snapshot a whole website from its URL (default command)")
    p.add_argument("url")
    p.add_argument("-o", "--out", default=None, help="output folder (default snapshots/<host>-<timestamp>)")
    p.add_argument("--name", default=None, help="display name used in reports (default: host)")
    p.add_argument("--max-pages", type=int, default=None, help="stop after this many HTML pages (default 200)")
    p.add_argument("--delay", type=float, default=None, help="seconds between requests (default 1.5)")
    p.add_argument("--viewports", nargs="*", default=None, choices=["desktop", "tablet", "mobile"])
    p.add_argument("--assets", action="store_true", help="also archive first-party CSS/JS/images/fonts")
    p.add_argument("--no-pdf", action="store_true", help="skip the screenshot PDF book")

    p = sub.add_parser("serve", help="local web UI")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--root", default="snapshots", help="folder where snapshots are written")

    p = sub.add_parser("discover", help="passive URL discovery (sitemaps, robots.txt, internal links)")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--only", nargs="*", default=None)
    p.add_argument("--fetch-pages", action="store_true", help="also GET HTML pages during discovery (adds a request per URL)")
    p = sub.add_parser("capture", help="rate-limited Playwright capture of manifest rows (restartable)")
    p.add_argument("--refs", nargs="*", default=None)
    p.add_argument("--viewports", nargs="*", default=None)
    p.add_argument("--max-pages", type=int, default=None)
    p.add_argument("--force", action="store_true")
    p.add_argument("--no-expand", action="store_true")
    sub.add_parser("qa", help="automated quality checks")
    sub.add_parser("integrity", help="SHA-256 hashes of every file")
    sub.add_parser("report", help="gallery, summary markdown + PDF")
    sub.add_parser("pdfbook", help="PDF with the report and every screenshot embedded")
    sub.add_parser("all", help="discover -> capture -> qa -> integrity -> report for an existing config")

    a = ap.parse_args(argv)

    if a.cmd == "run":
        from .pipeline import snapshot
        cfg = snapshot(a.url, a.out, name=a.name, max_pages=a.max_pages, delay_seconds=a.delay, viewports=a.viewports,
                       capture_assets=True if a.assets else None, pdf=not a.no_pdf)
        print(f"\nDone. Output: {cfg['_out']}\n  gallery : {cfg['_out'] / 'reports/visual-contact-sheets/index.html'}"
              f"\n  report  : {cfg['_out'] / 'summary.pdf'}")
        return
    if a.cmd == "serve":
        from .web import serve
        serve(a.host, a.port, a.root)
        return

    if not a.config:
        ap.error("--config is required for stage commands (or pass a URL to run everything)")
    cfg = load_config(a.config)
    if a.cmd == "discover":
        from .discover import run_discovery
        run_discovery(cfg, limit=a.limit, only=a.only, fetch_pages=a.fetch_pages)
    elif a.cmd == "capture":
        from .capture import run_capture
        run_capture(cfg, refs=a.refs, viewports=a.viewports, max_pages=a.max_pages, force=a.force, expand_links=not a.no_expand)
    elif a.cmd == "qa":
        from .qa import run_qa
        run_qa(cfg)
    elif a.cmd == "integrity":
        from .integrity import run_integrity
        run_integrity(cfg)
    elif a.cmd == "report":
        from .report import run_report
        run_report(cfg)
    elif a.cmd == "pdfbook":
        from .pdfbook import run_pdfbook
        run_pdfbook(cfg)
    elif a.cmd == "all":
        from .pipeline import snapshot
        snapshot(cfg["base_url"], cfg=cfg)


if __name__ == "__main__":
    main()
