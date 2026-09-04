"""Full evidence PDF: current-state report followed by every captured screenshot embedded as an image.

For each captured URL the desktop, tablet and mobile full-page screenshots are drawn side by side at a
common pixel scale (so layouts compare directly); pages taller than one sheet continue on the next
sheet with the three columns sliced at the same pixel rows. Interactive-state screenshots follow.
The Markdown current-state report is rendered through Chromium and prepended.
"""
from __future__ import annotations

import io
import json
import re
from pathlib import Path

import markdown
import pypdfium2 as pdfium
from PIL import Image
from playwright.sync_api import sync_playwright
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from .common import Log, ensure_dirs, load_manifest, utc_now

Image.MAX_IMAGE_PIXELS = None

PAGE_W, PAGE_H = A4
MARGIN = 12 * mm
HEADER_H = 26 * mm
USABLE_W = PAGE_W - 2 * MARGIN
JPEG_QUALITY = 58
MAX_COL_PX = {"desktop": 720, "tablet": 480, "mobile": 390}  # downsample before embedding

MD_CSS = """body{font:10.5pt/1.45 Helvetica,Arial,sans-serif;color:#111;margin:0}
h1{font-size:20pt;color:#b40000} h2{font-size:14pt;border-bottom:1px solid #ccc;margin-top:22pt}
table{border-collapse:collapse;font-size:9pt;width:100%} td,th{border:1px solid #bbb;padding:3px 6px;vertical-align:top;word-break:break-word}
th{background:#f0f0f0} code{font-size:8.5pt;background:#f4f4f4;padding:0 2px}
blockquote{border-left:4px solid #b40000;margin:0;padding:4px 10px;background:#fafafa}"""


def _md_to_pdf(md_path: Path, pdf_path: Path, footer: str) -> None:
    body = markdown.markdown(md_path.read_text(encoding="utf-8"), extensions=["tables", "fenced_code"])
    html_path = pdf_path.with_suffix(".html")
    html_path.write_text(f'<html><head><meta charset="utf-8"><style>{MD_CSS}</style></head><body>{body}</body></html>',
                         encoding="utf-8")
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page()
        pg.goto(html_path.resolve().as_uri())
        pg.pdf(path=str(pdf_path), format="A4", print_background=True, display_header_footer=True,
               margin={"top": "15mm", "bottom": "15mm", "left": "14mm", "right": "14mm"},
               header_template="<span></span>",
               footer_template=f"<div style='font-size:8px;width:100%;text-align:center;color:#666'>{footer} - page "
                               "<span class='pageNumber'></span>/<span class='totalPages'></span></div>")
        b.close()
    html_path.unlink(missing_ok=True)


def _open(path: Path, max_w: int) -> Image.Image | None:
    try:
        im = Image.open(path).convert("RGB")
    except Exception:
        return None
    if im.width > max_w:
        im = im.resize((max_w, max(1, round(im.height * max_w / im.width))), Image.LANCZOS)
    return im


def _reader(im: Image.Image) -> ImageReader:
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=JPEG_QUALITY, optimize=True)
    buf.seek(0)
    return ImageReader(buf)


def _header(c: canvas.Canvas, row: dict, cont: int, cfg: dict) -> None:
    y = PAGE_H - MARGIN
    c.setFont("Helvetica-Bold", 11)
    title = (row.get("page_title") or "").strip() or "(no title)"
    c.drawString(MARGIN, y - 10, f"{row['ref_id']}  {title[:95]}" + ("  (continued)" if cont else ""))
    c.setFont("Helvetica", 8)
    c.drawString(MARGIN, y - 21, f"URL: {row['normalized_url'][:130]}")
    caps = row.get("captures") or {}
    when = next((v.get("started_at") for v in caps.values() if v.get("started_at")), "-")
    cache = (row.get("cache_status") or "not disclosed").upper()
    final = row.get("final_url") or ""
    redirect = f"   redirected to {final[:70]}" if final and final != row["normalized_url"] else ""
    c.drawString(MARGIN, y - 31, f"HTTP {row.get('http_status')}   cache: {cache}   captured {when}   "
                 f"requests: {row.get('request_count', len(row.get('requests') or []))}{redirect}")
    heights = "   ".join(f"{vp}: {caps[vp].get('page_height', '?')}px" for vp in cfg["viewports"] if vp in caps)
    c.drawString(MARGIN, y - 41, f"Rendered heights: {heights}")
    c.setFont("Helvetica-Oblique", 7)
    c.setFillGray(0.4)
    c.drawRightString(PAGE_W - MARGIN, MARGIN / 2, f"{cfg['client']} - site snapshot - screenshot evidence")
    c.setFillGray(0)


def _column_page(c: canvas.Canvas, row: dict, cfg: dict, images: dict[str, Image.Image]) -> None:
    """Draw the viewport columns at one common scale, slicing across sheets if the tallest page overflows."""
    vps = [vp for vp in cfg["viewports"] if vp in images]
    src_w = {vp: cfg["viewports"][vp]["width"] for vp in vps}
    gap = 3 * mm
    scale = (USABLE_W - gap * (len(vps) - 1)) / sum(src_w.values())  # points per CSS pixel
    css_px = {vp: images[vp].width / src_w[vp] for vp in vps}  # image px per CSS px (after downsample)
    max_css_h = max(images[vp].height / css_px[vp] for vp in vps)
    avail_h = PAGE_H - MARGIN - HEADER_H - MARGIN - 6 * mm
    slice_css = avail_h / scale
    y_css = 0.0
    cont = 0
    while y_css < max_css_h:
        _header(c, row, cont, cfg)
        x = MARGIN
        top = PAGE_H - MARGIN - HEADER_H
        for vp in vps:
            im = images[vp]
            col_w = src_w[vp] * scale
            c.setFont("Helvetica-Bold", 7)
            c.drawString(x, top, f"{vp} {src_w[vp]}x{cfg['viewports'][vp]['height']}  full page")
            y0 = int(y_css * css_px[vp])
            y1 = int(min(y_css + slice_css, im.height / css_px[vp]) * css_px[vp])
            if y1 > y0:
                part = im.crop((0, y0, im.width, y1))
                h_pt = (y1 - y0) / css_px[vp] * scale
                c.drawImage(_reader(part), x, top - 4 - h_pt, width=col_w, height=h_pt)
                c.setLineWidth(0.3)
                c.rect(x, top - 4 - h_pt, col_w, h_pt)
            x += col_w + gap
        c.showPage()
        y_css += slice_css
        cont += 1


def _states_page(c: canvas.Canvas, row: dict, cfg: dict, files: list[Path]) -> None:
    _header(c, row, 0, cfg)
    top = PAGE_H - MARGIN - HEADER_H
    c.setFont("Helvetica-Bold", 8)
    c.drawString(MARGIN, top, "Interactive states (captured from the same navigation, no reload)")
    avail_h = top - 6 - MARGIN - 6 * mm
    x = MARGIN
    for f in files:
        im = _open(f, 900)
        if im is None:
            continue
        w = min(USABLE_W / 2 - 2 * mm, avail_h * im.width / im.height)
        h = w * im.height / im.width
        if x + w > PAGE_W - MARGIN:
            break
        c.drawImage(_reader(im), x, top - 10 - h, width=w, height=h)
        c.rect(x, top - 10 - h, w, h, stroke=1, fill=0)
        label = re.sub(r"^P\d+_.*?_(desktop|tablet|mobile)_", r"\1: ", f.stem)
        c.setFont("Helvetica", 7)
        c.drawString(x, top - 10 - h - 9, label[:60])
        x += w + 4 * mm
    c.showPage()


def _cover(c: canvas.Canvas, cfg: dict, rows: list[dict], n_pages: int, part: int, n_parts: int, chunk: list[dict]) -> None:
    c.setFont("Helvetica-Bold", 22)
    c.drawString(MARGIN, PAGE_H - 50 * mm, "Screenshot appendix" + (f" - part {part} of {n_parts}" if n_parts > 1 else ""))
    c.setFont("Helvetica", 11)
    vp_desc = ", ".join(f"{k} {v['width']}x{v['height']}" for k, v in cfg["viewports"].items())
    lines = [f"Site: {cfg['client']}", f"Website: {cfg['base_url']}",
             f"Generated (UTC): {utc_now()}", "",
             f"Captured HTML pages: {n_pages} in total; this part: {len(chunk)} "
             + (f"({chunk[0]['ref_id']} to {chunk[-1]['ref_id']})" if chunk else ""),
             ("Part 1 also contains the snapshot report" if n_parts > 1 else "Preceded by the snapshot report")
             + " (scope, method, coverage, cache observations, defects).", "",
             f"Each page: full-page screenshots ({vp_desc}) at one common scale,",
             "followed by interactive-state screenshots where captured. Pages taller than one sheet continue on the next sheet.",
             "Original PNG files are in screenshots/ inside the snapshot folder.",
             "Images here are JPEG re-encodings for the report; the PNG files are the originals."]
    y = PAGE_H - 65 * mm
    for ln in lines:
        c.drawString(MARGIN, y, ln)
        y -= 6 * mm
    c.showPage()


def _render_rows(c: canvas.Canvas, rows: list[dict], cfg: dict, out: Path, log: Log) -> None:
    for i, r in enumerate(rows, 1):
        images = {}
        states = []
        for vp, cap in (r.get("captures") or {}).items():
            for f in cap.get("files") or []:
                p = out / f
                if f.endswith("_full.png") and p.exists():
                    im = _open(p, MAX_COL_PX.get(vp, 720))
                    if im is not None:
                        images[vp] = im
                elif "/interactive-states/" in f and p.exists():
                    states.append(p)
        if images:
            _column_page(c, r, cfg, images)
        else:
            _header(c, r, 0, cfg)
            c.setFont("Helvetica", 9)
            c.drawString(MARGIN, PAGE_H - MARGIN - HEADER_H, "No full-page screenshot on disk for this row.")
            c.showPage()
        if states:
            _states_page(c, r, cfg, states)
        if i % 50 == 0:
            log("INFO", f"pdfbook: {i}/{len(rows)} pages rendered")


def run_pdfbook(cfg: dict, stem: str = "full-report-with-screenshots", rows_per_part: int = 210) -> list[Path]:
    """Writes <stem>.pdf (or <stem>-part1ofN.pdf for large sites); the first file also carries the snapshot report."""
    ensure_dirs(cfg)
    log = Log(cfg)
    out: Path = cfg["_out"]
    rows = [r for r in load_manifest(cfg) if r["content_type"] == "html" and r.get("captures")]
    rows.sort(key=lambda r: r["ref_id"])
    chunks = [rows[i:i + rows_per_part] for i in range(0, len(rows), rows_per_part)] or [[]]
    n_parts = len(chunks)

    report_pdf = out / "reports" / "_current-state-report.tmp.pdf"
    _md_to_pdf(out / "capture-summary.md", report_pdf, f"{cfg['client']} - site snapshot report")

    targets, info = [], []
    for k, chunk in enumerate(chunks, 1):
        appendix = out / "reports" / f"_screenshot-appendix-{k}.tmp.pdf"
        c = canvas.Canvas(str(appendix), pagesize=A4, pageCompression=1)
        c.setTitle(f"{cfg['client']} - site snapshot - full report with screenshots (part {k} of {n_parts})")
        _cover(c, cfg, rows, len(rows), k, n_parts, chunk)
        _render_rows(c, chunk, cfg, out, log)
        c.save()
        merged = pdfium.PdfDocument.new()
        for part in ([report_pdf] if k == 1 else []) + [appendix]:
            src = pdfium.PdfDocument(str(part))
            merged.import_pages(src, list(range(len(src))))
        target = out / (f"{stem}-part{k}of{n_parts}.pdf" if n_parts > 1 else f"{stem}.pdf")
        merged.save(str(target))
        appendix.unlink(missing_ok=True)
        log("INFO", f"pdfbook: {target.name} ({len(merged)} pages, {target.stat().st_size / 1e6:.1f} MB, "
                    f"{chunk[0]['ref_id']}-{chunk[-1]['ref_id']})" if chunk else f"pdfbook: {target.name}")
        info.append({"file": target.name, "pages": len(merged), "bytes": target.stat().st_size,
                     "first_ref": chunk[0]["ref_id"] if chunk else None, "last_ref": chunk[-1]["ref_id"] if chunk else None})
        targets.append(target)
    report_pdf.unlink(missing_ok=True)
    json.dump({"generated_at": utc_now(), "html_rows": len(rows), "parts": info},
              open(out / "reports" / f"{stem}.json", "w"), indent=2)
    return targets
