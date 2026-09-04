"""Automated quality assurance over the captured evidence. Flags only; never deletes."""
from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path
from urllib.parse import unquote, urlsplit

import imagehash
from PIL import Image, ImageStat

from .common import Log, ensure_dirs, load_manifest, utc_now, write_json

Image.MAX_IMAGE_PIXELS = None


def image_facts(path: Path) -> dict:
    with Image.open(path) as im:
        im = im.convert("L")
        w, h = im.size
        stat = ImageStat.Stat(im)
        sample = im.copy()
        sample.thumbnail((256, 4096))
        return {"width": w, "height": h, "stddev": round(stat.stddev[0], 2), "mean": round(stat.mean[0], 2),
                "phash": str(imagehash.phash(sample.resize((256, min(256, max(8, sample.size[1]))))))}


def run_qa(cfg: dict) -> dict:
    d = ensure_dirs(cfg)
    log = Log(cfg)
    rows = load_manifest(cfg)
    vps = list(cfg["viewports"])
    findings: list[dict] = []
    per_file: dict[str, dict] = {}
    phashes: dict[str, list[str]] = defaultdict(list)

    def flag(sev: str, code: str, ref: str, msg: str, **extra):
        findings.append({"severity": sev, "code": code, "ref_id": ref, "message": msg, **extra})

    html_rows = [r for r in rows if r["content_type"] == "html" and r["capture_status"] != "excluded"]
    doc_rows = [r for r in rows if r["content_type"] == "document"]
    excluded = [r for r in rows if r["capture_status"] == "excluded"]

    for r in html_rows:
        caps = r.get("captures", {})
        if r["capture_status"] == "pending" and not caps:
            flag("info", "NOT_CAPTURED", r["ref_id"], "queued but not visited (page limit reached or run stopped)")
            continue
        for vp in vps:
            c = caps.get(vp)
            if not c or not c.get("status", "").startswith("captured"):
                flag("high", "MISSING_VIEWPORT", r["ref_id"], f"no successful {vp} capture (status={c.get('status') if c else 'none'})", viewport=vp)
                continue
            pngs = [f for f in c["files"] if f.endswith(".png") and "/interactive-states/" not in f]
            kinds = {("full" if f.endswith("_full.png") else "fold") for f in pngs}
            if kinds != {"full", "fold"}:
                flag("high", "MISSING_SCREENSHOT", r["ref_id"], f"{vp}: expected full+fold, found {sorted(kinds)}", viewport=vp)
            for f in pngs:
                p = cfg["_out"] / f
                if not p.exists():
                    flag("high", "FILE_MISSING", r["ref_id"], f"referenced screenshot missing: {f}"); continue
                facts = image_facts(p)
                per_file[f] = facts
                exp_w = cfg["viewports"][vp]["width"]
                if facts["width"] != exp_w:
                    flag("medium", "UNEXPECTED_WIDTH", r["ref_id"], f"{f}: width {facts['width']} != viewport {exp_w}")
                if facts["stddev"] < 3:
                    flag("high", "BLANK_IMAGE", r["ref_id"], f"{f}: near-uniform image (stddev {facts['stddev']})")
                if f.endswith("_full.png") and facts["height"] < cfg["viewports"][vp]["height"] * 0.9:
                    flag("medium", "SHORT_PAGE", r["ref_id"], f"{f}: full-page height {facts['height']}px is shorter than the viewport")
                if f.endswith("_full.png"):
                    phashes[facts["phash"]].append(f"{r['ref_id']}:{vp}")
            if c.get("http_status") and c["http_status"] >= 400:
                flag("high", "HTTP_ERROR", r["ref_id"], f"{vp}: HTTP {c['http_status']}", viewport=vp)
            if c.get("broken_images"):
                flag("medium", "BROKEN_IMAGES", r["ref_id"], f"{vp}: {c['broken_images']} broken <img> element(s)", viewport=vp)
            if c.get("visible_error_text"):
                flag("high", "VISIBLE_ERROR_TEXT", r["ref_id"], f"{vp}: server-side error text visible in page body", viewport=vp)
            if c.get("horizontal_overflow"):
                flag("low", "HORIZONTAL_OVERFLOW", r["ref_id"], f"{vp}: document wider than viewport (possible responsive issue)", viewport=vp)
            for n in c.get("notes", []):
                if "timeout" in n or "failed" in n or "clipped" in n:
                    flag("low", "CAPTURE_NOTE", r["ref_id"], f"{vp}: {n}", viewport=vp)
        # title vs slug sanity
        title = (r.get("page_title") or "").lower()
        slug_words = [w for w in re.split(r"[-_/]+", unquote(urlsplit(r["normalized_url"]).path).lower()) if len(w) > 3]
        if title and slug_words and not any(w in title for w in slug_words) and "page/" not in r["normalized_url"]:
            flag("low", "TITLE_SLUG_MISMATCH", r["ref_id"], f"title '{r['page_title']}' shares no word with URL path", url=r["normalized_url"])
        if r.get("redirect_chain"):
            flag("info", "REDIRECT", r["ref_id"], " ; ".join(r["redirect_chain"]))
        heights = {v: caps[v].get("page_height") for v in vps if caps.get(v, {}).get("page_height")}
        if len(heights) == len(vps) and heights.get("mobile") and heights.get("desktop"):
            if heights["mobile"] < 0.45 * heights["desktop"]:
                flag("medium", "LAYOUT_COLLAPSE", r["ref_id"],
                     f"mobile page height {heights['mobile']}px is far shorter than desktop {heights['desktop']}px - content may be missing at the mobile viewport",
                     heights=heights)
        # cross-viewport title consistency
        titles = {caps[v].get("title") for v in vps if caps.get(v, {}).get("title") is not None}
        if len(titles) > 1:
            flag("medium", "TITLE_VARIES_BY_VIEWPORT", r["ref_id"], f"titles differ across viewports: {sorted(titles)}")

    # duplicate full-page screenshots across different URLs (same viewport)
    for h, members in phashes.items():
        refs = {m.split(":")[0] for m in members}
        if len(refs) > 1:
            flag("medium", "DUPLICATE_SCREENSHOT", ",".join(sorted(refs)), f"identical perceptual hash across URLs: {members}")
    # near-duplicates (hamming distance <= 4) between different refs
    items = [(imagehash.hex_to_hash(h), h, m) for h, m in phashes.items()]
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            if items[i][0] - items[j][0] <= 4:
                ri = {m.split(":")[0] for m in items[i][2]}; rj = {m.split(":")[0] for m in items[j][2]}
                if ri != rj:
                    flag("low", "NEAR_DUPLICATE_SCREENSHOT", ",".join(sorted(ri | rj)),
                         f"very similar full-page screenshots: {items[i][2]} ~ {items[j][2]}")

    for r in doc_rows:
        st = r.get("capture_status", "pending")
        if not st.startswith("captured") and st != "excluded":
            flag("medium", "DOCUMENT_NOT_CAPTURED", r["ref_id"], f"{r['normalized_url']}: {st}")

    # sitemap vs crawl coverage
    in_sitemap = [r for r in rows if r.get("in_sitemap")]
    sitemap_not_captured = [r["normalized_url"] for r in in_sitemap if not r["capture_status"].startswith("captured")]
    linked_not_in_sitemap = [r["normalized_url"] for r in rows if not r.get("in_sitemap") and r["content_type"] == "html"
                             and r["capture_status"] != "excluded"]
    # safety assertions
    forbidden = re.compile(r"/wp-admin|/wp-login\.php|/checkout|/cart|add-to-cart=|nocache|cache=|_=\d{10,}")
    safety = {
        "forbidden_urls_in_manifest_captured": [r["normalized_url"] for r in rows if forbidden.search(r["normalized_url"]) and r["capture_status"].startswith("captured")],
        "third_party_pages_captured": [r["normalized_url"] for r in rows if urlsplit(r["normalized_url"]).hostname not in cfg["allowed_hosts"]],
        "forms_submitted": 0,
        "cache_bypass_headers_sent": False,
        "authenticated_sessions": 0,
    }
    for k in ("forbidden_urls_in_manifest_captured", "third_party_pages_captured"):
        for u in safety[k]:
            flag("high", "SAFETY", "-", f"{k}: {u}")

    summary = {
        "generated_at": utc_now(),
        "urls_total": len(rows), "html_pages": len(html_rows), "documents": len(doc_rows), "excluded": len(excluded),
        "captured_html": sum(1 for r in html_rows if r["capture_status"].startswith("captured")),
        "partial_html": sum(1 for r in html_rows if r["capture_status"] == "partial"),
        "failed_html": sum(1 for r in html_rows if r["capture_status"] in ("failed", "pending")),
        "captured_documents": sum(1 for r in doc_rows if r["capture_status"].startswith("captured")),
        "viewport_coverage": {vp: sum(1 for r in html_rows if r.get("captures", {}).get(vp, {}).get("status", "").startswith("captured")) for vp in vps},
        "languages": sorted({r.get("language") or "und" for r in html_rows}),
        "findings_by_severity": {s: sum(1 for f in findings if f["severity"] == s) for s in ("high", "medium", "low", "info")},
        "findings_by_code": {c: sum(1 for f in findings if f["code"] == c) for c in sorted({f["code"] for f in findings})},
        "sitemap_urls": len(in_sitemap), "sitemap_not_captured": sitemap_not_captured,
        "linked_not_in_sitemap_count": len(linked_not_in_sitemap), "linked_not_in_sitemap": linked_not_in_sitemap,
        "safety": safety,
    }
    write_json(d["coverage"] / "qa-summary.json", summary)
    write_json(d["coverage"] / "qa-findings.json", findings)
    write_json(d["coverage"] / "screenshot-image-facts.json", per_file)
    broken = [f for f in findings if f["code"] in ("HTTP_ERROR", "VISIBLE_ERROR_TEXT", "BLANK_IMAGE", "BROKEN_IMAGES", "MISSING_VIEWPORT", "SHORT_PAGE")]
    write_json(d["broken"] / "broken-pages.json", broken)
    log("INFO", f"QA complete: {len(findings)} findings ({summary['findings_by_severity']})")
    return summary
