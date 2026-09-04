"""Reports: HTML review gallery, snapshot summary (Markdown + PDF), cache observations, exceptions, coverage."""
from __future__ import annotations

import html
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

from PIL import Image
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .common import Log, TOOL_VERSION, cache_status_from, ensure_dirs, load_manifest, utc_now

Image.MAX_IMAGE_PIXELS = None

DISCLAIMER = ("This snapshot records the publicly visible state of the website during the stated capture period, as seen by "
              "an ordinary unauthenticated visitor. It is intended for visual reference and before/after comparison. It is not "
              "a source-code, database or application backup, and it does not prove that the website was secure or fully "
              "functional at the time of capture.")


def _load(path: Path, default):
    return json.load(open(path, encoding="utf-8")) if path.exists() else default


def thumb(src: Path, dest: Path, width: int = 320, max_h: int = 1400) -> Path | None:
    if dest.exists() and dest.stat().st_mtime >= src.stat().st_mtime:
        return dest
    try:
        with Image.open(src) as im:
            im = im.convert("RGB")
            ratio = width / im.width
            h = min(int(im.height * ratio), max_h)
            im = im.resize((width, int(im.height * ratio)))
            im = im.crop((0, 0, width, h))
            dest.parent.mkdir(parents=True, exist_ok=True)
            im.save(dest, "JPEG", quality=70, optimize=True)
        return dest
    except Exception:
        return None


def cache_stats(rows: list[dict], cfg: dict, d: dict) -> dict:
    hits, misses, other, ages, servers, techs = Counter(), Counter(), Counter(), [], Counter(), Counter()
    per_page = []
    for r in rows:
        lang = (r.get('language') or 'und').split('-')[0].lower()
        single = d["headers"] / f"{r['ref_id']}_{r['slug']}.headers.json"
        if single.exists():
            sources = [("single-request", single)]
        else:  # pilot-era captures: one navigation per viewport
            sources = [(vp, d["headers"] / f"{r['ref_id']}_{r['slug']}_{lang}_{vp}.headers.json")
                       for vp in (r.get("captures") or {})]
        for vp, hp in sources:
            h = _load(hp, {})
            ch = {k.lower(): v for k, v in (h.get("cache_headers") or {}).items()}
            if not ch and not h:
                continue
            st = cache_status_from(ch)
            if st in ("hit", "revalidated", "stale", "updating"):
                hits[r["ref_id"]] += 1
            elif st in ("miss", "expired", "bypass", "dynamic", "no-cache"):
                misses[r["ref_id"]] += 1
            else:
                other[r["ref_id"]] += 1
            if "age" in ch:
                ages.append(int(ch["age"]) if str(ch["age"]).isdigit() else ch["age"])
            servers[ch.get("server", "-")] += 1
            for k in ch:
                if k.startswith("x-litespeed"): techs["LiteSpeed Cache (x-litespeed-*)"] += 1
                if k.startswith("cf-"): techs["Cloudflare (cf-*)"] += 1
                if k.startswith("x-qc"): techs["QUIC.cloud (x-qc-*)"] += 1
                if k in ("x-cache", "x-cache-status", "x-proxy-cache", "via", "x-vercel-cache", "x-served-by", "x-fastly-cache",
                         "x-varnish-cache", "x-nginx-cache", "x-sucuri-cache"): techs[f"Proxy/CDN header ({k})"] += 1
            per_page.append({"ref_id": r["ref_id"], "url": r["normalized_url"], "viewport": vp,
                             "cache_status": st, "age": ch.get("age"),
                             "etag": ch.get("etag"), "cache-control": ch.get("cache-control"),
                             "server": ch.get("server"), "platform": ch.get("platform"), "status": h.get("status")})
    return {"hits": hits, "misses": misses, "other": other, "ages": ages, "servers": servers, "techs": techs, "per_page": per_page}


def run_report(cfg: dict) -> None:
    d = ensure_dirs(cfg)
    log = Log(cfg)
    out: Path = cfg["_out"]
    rows = load_manifest(cfg)
    qa = _load(d["coverage"] / "qa-summary.json", {})
    findings = _load(d["coverage"] / "qa-findings.json", [])
    env = _load(out / "environment.json", {})
    integ = _load(d["integrity"] / "integrity-summary.json", {})
    vps = list(cfg["viewports"])
    by_ref = defaultdict(list)
    for f in findings:
        by_ref[f["ref_id"]].append(f)
    html_rows = [r for r in rows if r["content_type"] == "html" and r["capture_status"] != "excluded"]
    doc_rows = [r for r in rows if r["content_type"] == "document"]
    excluded = [r for r in rows if r["capture_status"] == "excluded"]
    times = [c.get("started_at") for r in rows for c in (r.get("captures") or {}).values() if c.get("started_at")]
    t_start, t_end = (min(times), max(times)) if times else ("-", "-")
    cs = cache_stats(rows, cfg, d)
    req_total = sum(len(r.get("requests") or []) for r in rows)
    generators = Counter()
    for r in rows:
        if r["content_type"] == "html" and str(r["capture_status"]).startswith("captured"):
            body = d["html_response"] / f"{r['ref_id']}_{r['slug']}.html"
            if body.exists():
                m = re.search(r'<meta[^>]+name=["\']generator["\'][^>]+content=["\']([^"\']+)', body.read_text(errors="ignore"), re.I)
                if m:
                    generators[m.group(1).split(";")[0].strip()] += 1

    # ------------------------------------------------------------- gallery
    tdir = d["contact"] / "thumbs"
    cards = []
    for r in rows:
        if r["capture_status"] == "excluded":
            continue
        caps = r.get("captures") or {}
        cells = []
        for vp in vps:
            c = caps.get(vp, {})
            full = next((f for f in c.get("files", []) if f.endswith(f"_{vp}_full.png")), None)
            if full:
                t = thumb(out / full, tdir / (Path(full).stem + ".jpg"))
                rel_full = html.escape("../../" + full)
                rel_t = html.escape(str(t.relative_to(d["contact"]))) if t else ""
                cells.append(f'<div class="shot"><a href="{rel_full}" target="_blank"><img loading="lazy" src="{rel_t}" alt="{vp}"></a>'
                             f'<div class="cap">{vp} · {c.get("page_height", "?")}px · HTTP {c.get("http_status", "?")}</div></div>')
            else:
                cells.append(f'<div class="shot missing">{vp}<br>{html.escape(c.get("status", "not captured"))}</div>')
        inter = [f for vp in vps for f in caps.get(vp, {}).get("files", []) if "/interactive-states/" in f]
        inter_html = "".join(f'<a href="{html.escape("../../" + f)}" target="_blank">{html.escape(Path(f).stem.split("_", 4)[-1])}</a> ' for f in inter)
        flags = by_ref.get(r["ref_id"], []) + [f for f in findings if r["ref_id"] in f["ref_id"].split(",") and f["ref_id"] != r["ref_id"]]
        flag_html = "".join(f'<li class="{f["severity"]}">[{f["severity"]}] {html.escape(f["code"])}: {html.escape(f["message"][:220])}</li>' for f in flags)
        notes = "; ".join(sorted({n for vp in vps for n in caps.get(vp, {}).get("notes", [])}))
        doc_html = ""
        if r["content_type"] == "document":
            dc = caps.get("document", {})
            files = dc.get("files", [])
            doc_html = f'<div class="doc">Document: {html.escape(dc.get("content_type") or "")} {dc.get("bytes", "")} bytes ' + \
                       "".join(f'<a href="{html.escape("../../" + f)}" target="_blank">open</a>' for f in files) + "</div>"
        cards.append(f"""
<section class="card" id="{r['ref_id']}" data-status="{html.escape(r['capture_status'])}">
  <header><span class="ref">{r['ref_id']}</span> <span class="title">{html.escape(r.get('page_title') or '(no title)')}</span>
   <span class="status s-{html.escape(r['capture_status'].split('-')[0])}">{html.escape(r['capture_status'])}</span></header>
  <div class="url"><a href="{html.escape(r['normalized_url'])}" target="_blank" rel="noopener">{html.escape(r['normalized_url'])}</a>
   <span class="meta">lang={html.escape(r.get('language') or 'und')} · type={r['content_type']} · http={r.get('http_status')} · cache={html.escape(str(r.get('cache_status') or '-'))} · requests={r.get('request_count', 0)} · source={html.escape(', '.join(r['discovery_source'][:2]))}{' · IN SITEMAP' if r.get('in_sitemap') else ''}</span></div>
  {'<div class="shots">' + ''.join(cells) + '</div>' if r['content_type'] == 'html' else doc_html}
  {f'<div class="inter">Interactive states: {inter_html}</div>' if inter else ''}
  {f'<div class="notes">Notes: {html.escape(notes)}</div>' if notes else ''}
  {f'<ul class="flags">{flag_html}</ul>' if flag_html else ''}
</section>""")
    gallery = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Site snapshot – {html.escape(cfg['client'])}</title>
<style>
body{{font:14px/1.4 system-ui,sans-serif;margin:0;background:#f4f4f5;color:#111}} .top{{background:#111;color:#fff;padding:16px 24px;position:sticky;top:0;z-index:2}}
.top h1{{margin:0 0 4px;font-size:18px}} .top .sub{{color:#bbb;font-size:12px}} .top input{{margin-left:16px;padding:4px 8px;border-radius:4px;border:1px solid #444}}
.card{{background:#fff;margin:16px 24px;padding:12px 16px;border-radius:8px;box-shadow:0 1px 3px rgba(0,0,0,.08)}}
.card header{{display:flex;gap:12px;align-items:center}} .ref{{font-weight:700;font-family:monospace}} .title{{font-weight:600;flex:1}}
.status{{font-size:12px;padding:2px 8px;border-radius:10px;background:#ddd}} .s-captured{{background:#d1fae5}} .s-partial,.s-pending{{background:#fef3c7}} .s-failed{{background:#fecaca}}
.url{{font-size:12px;color:#555;margin:4px 0 10px;word-break:break-all}} .url .meta{{margin-left:8px;color:#888}}
.shots{{display:flex;gap:12px;align-items:flex-start}} .shot img{{width:320px;border:1px solid #ddd;display:block}} .shot .cap{{font-size:11px;color:#666;margin-top:4px}}
.shot.missing{{width:320px;height:120px;border:1px dashed #f87171;color:#b91c1c;display:flex;align-items:center;justify-content:center;text-align:center}}
.flags{{margin:8px 0 0;padding-left:18px;font-size:12px}} .flags .high{{color:#b91c1c}} .flags .medium{{color:#b45309}} .flags .low,.flags .info{{color:#555}}
.notes,.inter,.doc{{font-size:12px;color:#444;margin-top:6px}} .summary{{margin:16px 24px;font-size:13px}} .summary td{{padding:2px 12px 2px 0}}
</style></head><body>
<div class="top"><h1>Site snapshot – {html.escape(cfg['client'])} ({html.escape(cfg['base_url'])})</h1>
<div class="sub">Generated {utc_now()} · capture window {t_start} → {t_end} · {env.get('browser_name','')} {env.get('browser_version','')} · viewports {', '.join(f"{k} {v['width']}×{v['height']}" for k,v in cfg['viewports'].items())}
<input id="q" placeholder="filter by ref / title / URL" oninput="for(const c of document.querySelectorAll('.card'))c.style.display=c.textContent.toLowerCase().includes(this.value.toLowerCase())?'':'none'"></div></div>
<div class="summary"><table>
<tr><td>URLs discovered</td><td><b>{len(rows)}</b></td><td>HTML pages</td><td><b>{len(html_rows)}</b></td><td>Documents</td><td><b>{len(doc_rows)}</b></td><td>Excluded</td><td><b>{len(excluded)}</b></td></tr>
<tr><td>Captured (all viewports)</td><td><b>{qa.get('captured_html','?')}</b></td><td>Partial</td><td><b>{qa.get('partial_html','?')}</b></td><td>Failed/pending</td><td><b>{qa.get('failed_html','?')}</b></td><td>QA findings</td><td><b>{sum((qa.get('findings_by_severity') or {}).values())}</b></td></tr>
</table><p>{html.escape(DISCLAIMER)}</p></div>
{''.join(cards)}
<div class="summary">Excluded URLs (recorded, not captured): <ul>{''.join(f'<li>{html.escape(r["ref_id"])} {html.escape(r["normalized_url"])} – {html.escape(r.get("notes") or "")}</li>' for r in excluded)}</ul></div>
</body></html>"""
    (d["contact"] / "index.html").write_text(gallery, encoding="utf-8")

    # ------------------------------------------------------------- cache observations
    lines = [f"# Cache observations – {cfg['client']}", "", f"Generated {utc_now()} (UTC). Observations from response headers only.", "",
             "## Apparent cache / CDN technology", ""]
    for k, v in cs["techs"].most_common():
        lines.append(f"- {k}: seen on {v} responses")
    if not cs["techs"]:
        lines.append("- no recognisable cache/CDN headers observed")
    lines += ["", f"- `server` header values: {dict(cs['servers'])}", "",
              "## HIT / MISS summary", "",
              f"- Pages with a cache HIT: {len(cs['hits'])}", f"- Pages with a MISS: {len(cs['misses'])}",
              f"- Pages with no disclosed cache status: {len(cs['other'])}", "",
              "## Per-response detail", "",
              "| Ref | HTTP | cache status | age | etag | cache-control |", "|---|---|---|---|---|---|"]
    for p in cs["per_page"]:
        lines.append(f"| {p['ref_id']} | {p['status']} | {p['cache_status'] or '-'} | {p['age'] or '-'} | `{p['etag'] or '-'}` | {p['cache-control'] or '-'} |")
    (out / "cache-observations.md").write_text("\n".join(lines), encoding="utf-8")

    # ------------------------------------------------------------- exceptions
    ex = [f"# Exception register – {cfg['client']}", "", f"Generated {utc_now()} (UTC).", "",
          "| Ref | URL | Type | Status | Reason / note |", "|---|---|---|---|---|"]
    n_ex = 0
    for r in rows:
        st = r["capture_status"]
        caps = r.get("captures") or {}
        problems = [n for c in caps.values() for n in c.get("notes", []) if any(k in n for k in ("failed", "timeout", "clipped", "error", "fallback"))]
        if st != "captured" or problems:
            n_ex += 1
            reason = r.get("notes") or ""
            if problems:
                reason = (reason + " | " if reason else "") + "; ".join(sorted(set(problems)))
            ex.append(f"| {r['ref_id']} | {r['normalized_url']} | {r['content_type']} | {st} | {reason.replace('|', '/')} |")
    if n_ex == 0:
        ex.append("| – | – | – | – | no exceptions |")
    ex += ["", "## What a static snapshot cannot preserve", "",
           "- Third-party embeds (chat widgets, maps, videos, analytics) are captured visually; their remote resources are not archived.",
           "- Server-side functionality (search, forms, accounts, cart/checkout) is not exercised; only the visible interface is preserved.",
           "- Sliders/carousels and animated content are captured at a single settled moment per viewport."]
    (out / "exceptions.md").write_text("\n".join(ex), encoding="utf-8")

    # ------------------------------------------------------------- crawl coverage
    cov = [f"# Crawl coverage – {cfg['client']}", "",
           f"- Sitemap URLs: {qa.get('sitemap_urls', 0)}", f"- Sitemap URLs not captured: {len(qa.get('sitemap_not_captured', []))}"]
    cov += [f"  - {u}" for u in qa.get("sitemap_not_captured", [])]
    cov += [f"- Internally linked HTML URLs absent from the sitemap: {qa.get('linked_not_in_sitemap_count', 0)}"]
    cov += [f"  - {u}" for u in qa.get("linked_not_in_sitemap", [])]
    cov += ["", "## Discovery sources", ""]
    src = Counter(s.split(":")[0] for r in rows for s in r["discovery_source"])
    cov += [f"- {k}: {v}" for k, v in src.most_common()]
    (d["coverage"] / "crawl-coverage.md").write_text("\n".join(cov), encoding="utf-8")

    # ------------------------------------------------------------- summary report
    langs = Counter((r.get("language") or "und") for r in html_rows)
    defects = [f for f in findings if f["severity"] in ("high", "medium")]
    vp_desc = ", ".join(f"{k} {v['width']}×{v['height']}" for k, v in cfg["viewports"].items())
    rep = [f"# Site snapshot report – {cfg['client']}", "",
           f"**Website:** {cfg['base_url']}  ", f"**Capture window (UTC):** {t_start} → {t_end}  ",
           f"**Report generated (UTC):** {utc_now()}  ",
           f"**Tool:** {TOOL_VERSION}, Playwright {env.get('playwright_version', '?')}, {env.get('browser_name', '?')} {env.get('browser_version', '?')}", "",
           "> " + DISCLAIMER, "",
           "## 1. Scope", "",
           f"Publicly accessible first-party HTML pages and linked public documents on `{', '.join(cfg['allowed_hosts'])}`, discovered from the seed URL, "
           "`robots.txt`, XML sitemaps and internal links. Administrative, authenticated, cart/checkout, feed and state-changing URLs are recorded but not captured (see `exceptions.md`).", "",
           "## 2. Method", "",
           "1. Passive discovery (robots.txt and sitemaps); new same-site links found in rendered pages are queued for their own visit.",
           f"2. Each page gets one browser navigation in a clean headless Chromium context; tablet/mobile views are produced by resizing the loaded page ({vp_desc}).",
           "3. Per page and viewport: above-the-fold and full-page PNG (after scrolling for lazy-loaded media), response HTML, rendered DOM, headers, redirect chain, page metadata, first-party assets (optional), safe interactive states (menu, search, cookie banner).",
           f"4. Rate limiting: concurrency {cfg['crawl']['concurrency']}, {cfg['crawl']['delay_seconds']}s delay, {cfg['crawl']['max_retries']} retries, hard stop on HTTP 429 or {cfg['crawl']['stop_after_consecutive_5xx']} consecutive 5xx.",
           "5. Automated QA (blank/duplicate screenshots, broken images, error text, layout collapse) and SHA-256 hashing of every file.", "",
           "## 3. Coverage", "",
           "| Metric | Value |", "|---|---|",
           f"| URLs discovered | {len(rows)} |", f"| HTML pages in scope | {len(html_rows)} |",
           f"| HTML pages fully captured | {qa.get('captured_html', 0)} |", f"| Partially captured | {qa.get('partial_html', 0)} |",
           f"| Failed / not captured | {qa.get('failed_html', 0)} |", f"| Documents discovered / archived | {len(doc_rows)} / {qa.get('captured_documents', 0)} |",
           f"| Excluded (out of scope) | {len(excluded)} |", f"| Total requests | {req_total} |",
           f"| Files hashed | {integ.get('files_hashed', '?')} ({(integ.get('total_bytes', 0) or 0) / 1e6:.1f} MB) |", "",
           "### Languages", "", *[f"- `{k}`: {v} pages" for k, v in langs.most_common()], "",
           "### Viewport coverage", "", *[f"- {vp}: {n} / {len(html_rows)} pages" for vp, n in (qa.get('viewport_coverage') or {}).items()], "",
           "### Detected platform", "", *([f"- `{k}`: {v} pages" for k, v in generators.most_common()] or ["- no generator meta tag found"]), "",
           "## 4. Cache observations", "",
           f"{len(cs['hits'])} pages served with a cache HIT, {len(cs['misses'])} with a MISS, {len(cs['other'])} without a disclosed status. "
           f"Apparent technology: {', '.join(k for k, _ in cs['techs'].most_common()) or 'none disclosed'}. Details in `cache-observations.md`.", "",
           "## 5. Visible defects and observations (recorded as-is)", "",
           *([f"- **{f['code']}** {f['ref_id']}: {f['message']}" for f in defects] or ["- No high/medium QA findings."]),
           "", f"Full list: `reports/crawl-coverage/qa-findings.json` ({sum((qa.get('findings_by_severity') or {}).values())} findings).", "",
           "## 6. Limitations", "",
           "- Evidence reflects what a first-time, unauthenticated visitor using headless Chromium received during the capture window.",
           "- Third-party resources, server-side functionality, forms, accounts and checkout were not exercised or archived.",
           "- Full-page screenshots taller than 16,000 px are clipped and noted in the manifest.",
           "- Animated or time-dependent content may legitimately differ between captures.", "",
           "## 7. Exceptions", "", f"{n_ex} entries – see `exceptions.md`."]
    (out / "capture-summary.md").write_text("\n".join(rep), encoding="utf-8")

    # ------------------------------------------------------------- README
    readme = f"""# Site snapshot – {cfg['client']}

Read-only capture of the public website `{cfg['base_url']}` taken {t_start} → {t_end} (UTC).

> {DISCLAIMER}

| Path | Contents |
|---|---|
| `capture-summary.md` / `summary.pdf` | Snapshot report (scope, method, coverage, defects, limitations) |
| `full-report-with-screenshots*.pdf` | Report plus every screenshot embedded (run `sitesnap pdfbook`) |
| `url-manifest.csv` / `.json` | Every discovered URL: reference ID, source, status, redirects, screenshots, notes |
| `evidence-register.csv`, `integrity/SHA256SUMS.txt` | Per-file SHA-256 hashes |
| `screenshots/<viewport>/full-page|above-fold/` | PNG screenshots, `P0001_home_en_desktop_full.png` naming |
| `screenshots/interactive-states/` | Menu, search, hover and cookie-banner states |
| `html/response/`, `html/rendered/` | Original response HTML and rendered DOM per viewport |
| `assets/` | First-party CSS/JS/images/fonts/documents (if enabled) |
| `metadata/` | Headers, redirect chains, link inventories, page metadata |
| `reports/visual-contact-sheets/index.html` | Review gallery |
| `reports/crawl-coverage/` | Sitemap/robots copies, QA results |
| `capture.config.json` | The exact configuration used (re-run with `sitesnap --config capture.config.json capture`) |

Verify: `sha256sum -c integrity/SHA256SUMS.txt`. Generated by {TOOL_VERSION} on {utc_now()}.
"""
    (out / "README.md").write_text(readme, encoding="utf-8")

    # ------------------------------------------------------------- summary PDF
    styles = getSampleStyleSheet()
    body = ParagraphStyle("b", parent=styles["BodyText"], fontSize=9.5, leading=13)
    h = styles["Heading2"]; h.fontSize = 12
    doc = SimpleDocTemplate(str(out / "summary.pdf"), pagesize=A4, leftMargin=18*mm, rightMargin=18*mm, topMargin=16*mm, bottomMargin=16*mm,
                            title=f"Site snapshot – {cfg['client']}", author=TOOL_VERSION)
    story = [Paragraph(f"Site snapshot – {html.escape(cfg['client'])}", styles["Title"]),
             Paragraph(f"<b>Website:</b> {html.escape(cfg['base_url'])} &nbsp; <b>Capture window (UTC):</b> {t_start} → {t_end}", body),
             Spacer(1, 6), Paragraph("What was captured", h),
             Paragraph(f"Every publicly reachable first-party page found from the seed URL, robots.txt, XML sitemap and internal links was rendered in a clean, unauthenticated Chromium browser at {html.escape(vp_desc)}. For each page: above-the-fold and full-page screenshots, response and rendered HTML, HTTP headers and redirects, page metadata and safe interactive states. Linked public documents were archived. Nothing on the website was changed: no login, no form submission, one request at a time with delays.", body),
             Paragraph("Coverage", h)]
    tbl = [["Metric", "Value"],
           ["URLs discovered", str(len(rows))], ["HTML pages in scope", str(len(html_rows))],
           ["Pages fully captured", str(qa.get("captured_html", 0))],
           ["Partially captured / failed", f"{qa.get('partial_html', 0)} / {qa.get('failed_html', 0)}"],
           ["Documents archived", f"{qa.get('captured_documents', 0)} of {len(doc_rows)}"],
           ["Excluded (admin, account, cart, feeds)", str(len(excluded))],
           ["Languages", ", ".join(f"{k} ({v})" for k, v in langs.most_common())],
           ["QA findings (high / medium / low+info)", f"{(qa.get('findings_by_severity') or {}).get('high', 0)} / {(qa.get('findings_by_severity') or {}).get('medium', 0)} / {(qa.get('findings_by_severity') or {}).get('low', 0) + (qa.get('findings_by_severity') or {}).get('info', 0)}"],
           ["Cache status disclosed", f"{len(cs['hits'])} pages HIT, {len(cs['misses'])} pages MISS"],
           ["Browser", f"{env.get('browser_name', '?')} {env.get('browser_version', '?')} (Playwright {env.get('playwright_version', '?')})"]]
    t = Table(tbl, colWidths=[80*mm, 90*mm])
    t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#222222")), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                           ("FONTSIZE", (0, 0), (-1, -1), 9), ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
                           ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.whitesmoke, colors.white])]))
    story += [t, Spacer(1, 6), Paragraph("Visible defects (recorded, not fixed)", h)]
    story += [Paragraph(html.escape(f"{f['code']} {f['ref_id']}: {f['message'][:200]}"), body) for f in defects[:25]] or [Paragraph("No high/medium QA findings.", body)]
    if len(defects) > 25:
        story.append(Paragraph(f"... and {len(defects) - 25} more in capture-summary.md", body))
    story += [Paragraph("What this snapshot is not", h), Paragraph(html.escape(DISCLAIMER), body),
              Spacer(1, 6), Paragraph(f"Generated {utc_now()} by {TOOL_VERSION}.", ParagraphStyle("f", parent=body, fontSize=8, textColor=colors.grey))]
    doc.build(story)
    log("INFO", "reports generated: gallery, capture-summary.md, summary.pdf, cache-observations.md, exceptions.md, README.md, crawl-coverage.md")
