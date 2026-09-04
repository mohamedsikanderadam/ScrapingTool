"""Playwright-based read-only page capture (screenshots, HTML, headers, assets, metadata)."""
from __future__ import annotations

import hashlib
from collections import deque
import json
import mimetypes
import platform
import re
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import unquote, urlsplit

from playwright.sync_api import Error as PWError, Page, TimeoutError as PWTimeout, sync_playwright

from .common import (Log, ManifestIndex, TOOL_VERSION, cache_status_from, ensure_dirs, is_allowed, load_manifest, normalize_url, record_request,
                     polite_sleep, save_manifest, short_hash, utc_now, write_json)

CACHE_HEADERS = ["x-litespeed-cache", "x-litespeed-cache-control", "x-litespeed-tag", "x-lsadc-cache",
                 "x-qc-cache", "x-qc-pop", "cf-cache-status", "cf-ray", "x-cache", "x-cache-status",
                 "age", "cache-control", "expires", "etag", "last-modified", "via", "server",
                 "platform", "x-powered-by", "vary", "x-turbo-charged-by", "x-proxy-cache"]

MAX_SHOT_HEIGHT = 16000  # Chromium's practical full-page capture ceiling


class Evidence:
    """Append-only evidence ledger; hashing happens later in the integrity step."""

    def __init__(self, cfg: dict):
        self.path = ensure_dirs(cfg)["state"] / "evidence.jsonl"

    def add(self, ref_id: str, path: Path, url: str, profile: str, kind: str, cfg: dict) -> None:
        rel = str(path.relative_to(cfg["_out"]))
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ref_id": ref_id, "relative_path": rel, "source_url": url,
                                 "capture_profile": profile, "kind": kind,
                                 "captured_at": utc_now(), "tool": TOOL_VERSION}) + "\n")


def asset_bucket(ct: str, url: str) -> str:
    p = urlsplit(url).path.lower()
    if ct.startswith("image/") or p.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico", ".avif")):
        return "images"
    if "css" in ct or p.endswith(".css"):
        return "styles"
    if "javascript" in ct or p.endswith((".js", ".mjs")):
        return "scripts"
    if "font" in ct or p.endswith((".woff", ".woff2", ".ttf", ".otf", ".eot")):
        return "fonts"
    if "pdf" in ct or p.endswith((".pdf", ".doc", ".docx", ".xls", ".xlsx", ".zip")):
        return "documents"
    return "other"


def asset_filename(url: str, ct: str) -> str:
    p = unquote(urlsplit(url).path)
    name = Path(p).name or "index"
    if "." not in name:
        ext = mimetypes.guess_extension(ct.split(";")[0].strip()) or ""
        name += ext
    name = re.sub(r"[^\w.\-]+", "_", name)[:120]
    return f"{short_hash(url)}_{name}"


def scroll_through(page: Page, step: int, pause_ms: int, max_steps: int = 400) -> int:
    """Scroll to the bottom in steps so lazy-loaded media renders, then return to the top."""
    height = page.evaluate("() => document.documentElement.scrollHeight")
    pos, steps = 0, 0
    while pos < height and steps < max_steps:
        pos += step
        page.evaluate(f"() => window.scrollTo(0, {pos})")
        page.wait_for_timeout(pause_ms)
        height = page.evaluate("() => document.documentElement.scrollHeight")
        steps += 1
    # entrance-animation gates (Elementor, WOW.js, AOS) hide content until scrolled into view;
    # give them time to release before returning to the top
    for _ in range(12):
        pending = page.evaluate(
            "() => document.querySelectorAll('.elementor-invisible, .wow:not(.animated), [data-aos]:not(.aos-animate)').length")
        if not pending:
            break
        page.wait_for_timeout(500)
    page.evaluate("() => window.scrollTo(0, 0)")
    page.wait_for_timeout(pause_ms * 2)
    return height


PAGE_DATA_JS = """
() => {
  const q = (s) => Array.from(document.querySelectorAll(s));
  const meta = (n) => (document.querySelector(`meta[name="${n}"]`) || document.querySelector(`meta[property="${n}"]`) || {}).content || null;
  const og = {};
  q('meta[property^="og:"]').forEach(m => { og[m.getAttribute('property')] = m.content; });
  const ld = q('script[type="application/ld+json"]').map(s => { try { return JSON.parse(s.textContent); } catch (e) { return {"_unparsed": s.textContent.slice(0, 2000)}; } });
  const imgs = q('img');
  const broken = imgs.filter(i => i.complete && i.naturalWidth === 0 && i.getAttribute('src')).map(i => i.currentSrc || i.src).slice(0, 200);
  const links = q('a[href]').map(a => a.href);
  const host = location.hostname.replace(/^www\\./, '');
  const internal = links.filter(h => { try { return new URL(h).hostname.replace(/^www\\./, '') === host; } catch (e) { return false; } });
  const external = links.filter(h => { try { const u = new URL(h); return u.protocol.startsWith('http') && u.hostname.replace(/^www\\./, '') !== host; } catch (e) { return false; } });
  return {
    title: document.title, lang: document.documentElement.lang || null,
    canonical: (document.querySelector('link[rel="canonical"]') || {}).href || null,
    meta_description: meta('description'), robots: meta('robots'), generator: meta('generator'),
    open_graph: og, twitter_card: meta('twitter:card'), json_ld: ld,
    hreflang: q('link[rel="alternate"][hreflang]').map(l => ({hreflang: l.hreflang, href: l.href})),
    image_count: imgs.length, broken_images: broken,
    iframes: q('iframe').map(f => f.src).filter(Boolean).slice(0, 50),
    videos: q('video, video source').map(v => v.src || v.currentSrc).filter(Boolean).slice(0, 50),
    forms: q('form').map(f => ({action: f.action, method: f.method, fields: Array.from(f.elements).map(e => e.name || e.id || e.type).slice(0, 40)})).slice(0, 20),
    internal_links: Array.from(new Set(internal)), external_links: Array.from(new Set(external)),
    body_text_length: (document.body && document.body.innerText || '').length,
    body_text_excerpt: (document.body && document.body.innerText || '').replace(/\\s+/g, ' ').slice(0, 600),
    page_height: document.documentElement.scrollHeight, page_width: document.documentElement.scrollWidth,
    has_horizontal_overflow: document.documentElement.scrollWidth > window.innerWidth + 1,
    visible_error_text: (document.body && /fatal error|warning:|notice:|there has been a critical error|database error/i.test(document.body.innerText)) || false,
    wp_theme: (q('link[href*="/wp-content/themes/"]').map(l => (l.href.match(/themes\\/([^\\/]+)/) || [])[1]).filter(Boolean)),
    wp_plugins: Array.from(new Set(q('link[href*="/wp-content/plugins/"], script[src*="/wp-content/plugins/"]').map(l => ((l.href || l.src).match(/plugins\\/([^\\/]+)/) || [])[1]).filter(Boolean))),
  };
}
"""


def first_visible(page: Page, selectors: list[str]):
    for sel in selectors:
        try:
            loc = page.locator(sel)
            for i in range(min(loc.count(), 6)):
                if loc.nth(i).is_visible(timeout=800):
                    return sel, loc.nth(i)
        except PWError:
            continue
    return None, None


def capture_viewport(page: Page, row: dict, vp_name: str, vp: dict, cfg: dict, d: dict, log: Log,
                     ev: Evidence, main_resp: dict, seen_responses: list[dict]) -> dict:
    """Screenshots, rendered DOM, page data and safe interactive states at one viewport.
    The page is already loaded; the viewport is resized in place - no navigation, no request."""
    crawl = cfg["crawl"]
    url, ref, slug = row["normalized_url"], row["ref_id"], row["slug"]
    lang = (row.get("language") or "und").split("-")[0].lower() or "und"
    base = f"{ref}_{slug}_{lang}_{vp_name}"
    result = {"viewport": vp_name, "started_at": utc_now(), "status": "pending", "files": [], "notes": []}
    try:
        page.set_viewport_size({"width": vp["width"], "height": vp["height"]})
        page.evaluate("() => window.scrollTo(0, 0)")
        page.wait_for_timeout(crawl["settle_wait_ms"] // 2)

        cb = cfg.get("cookie_banner", {})
        sel, loc = first_visible(page, cb.get("dismiss_selectors", []))
        if sel:
            p = d["interactive"] / f"{base}_cookie_banner_initial.png"
            page.screenshot(path=str(p), full_page=False)
            ev.add(ref, p, url, vp_name, "interactive", cfg)
            result["files"].append(str(p.relative_to(cfg["_out"])))
            loc.click()
            page.wait_for_timeout(800)
            result["notes"].append(f"cookie banner dismissed via {sel}")
            log("MANUAL", f"{ref} {vp_name}: cookie banner dismissed via selector {sel}")

        p_fold = d[f"shots_{vp_name}_fold"] / f"{base}_fold.png"
        page.screenshot(path=str(p_fold), full_page=False)
        ev.add(ref, p_fold, url, vp_name, "screenshot_fold", cfg)
        result["files"].append(str(p_fold.relative_to(cfg["_out"])))

        height = scroll_through(page, crawl["scroll_step_px"], crawl["scroll_pause_ms"])
        page.wait_for_timeout(crawl["settle_wait_ms"] // 2)
        p_full = d[f"shots_{vp_name}_full"] / f"{base}_full.png"
        try:
            if height > MAX_SHOT_HEIGHT:
                page.screenshot(path=str(p_full), clip={"x": 0, "y": 0, "width": vp["width"], "height": MAX_SHOT_HEIGHT},
                                full_page=True, animations="disabled")
                result["notes"].append(f"page height {height}px exceeds {MAX_SHOT_HEIGHT}px; full-page capture clipped")
            else:
                page.screenshot(path=str(p_full), full_page=True, timeout=90000, animations="disabled")
        except PWError as e:
            result["notes"].append(f"full-page screenshot failed: {str(e)[:200]}")
            page.screenshot(path=str(p_full), full_page=False)
            result["notes"].append("fallback: viewport-only image saved as *_full.png")
        ev.add(ref, p_full, url, vp_name, "screenshot_full", cfg)
        result["files"].append(str(p_full.relative_to(cfg["_out"])))

        rendered = page.content()
        p_r = d["html_rendered"] / f"{base}.html"
        p_r.write_text(rendered, encoding="utf-8")
        ev.add(ref, p_r, url, vp_name, "html_rendered", cfg)
        result["files"].append(str(p_r.relative_to(cfg["_out"])))
        data = page.evaluate(PAGE_DATA_JS)
        data.update({"ref_id": ref, "url": url, "final_url": page.url, "viewport": vp_name,
                     "viewport_size": vp, "captured_at": utc_now(),
                     "http_status": main_resp.get("status"), "cache_headers": main_resp.get("cache_headers"),
                     "single_navigation": True,
                     "first_party_responses": len([r for r in seen_responses if r["first_party"]]),
                     "third_party_hosts": sorted({urlsplit(r["url"]).hostname for r in seen_responses if not r["first_party"] and urlsplit(r["url"]).hostname}),
                     "failed_subresources": [r for r in seen_responses if r["first_party"] and r["status"] >= 400][:100]})
        write_json(d["page_data"] / f"{base}.json", data)
        result.update({"title": data["title"], "lang": data["lang"], "final_url": page.url,
                       "http_status": main_resp.get("status"), "page_height": height,
                       "broken_images": len(data["broken_images"]), "visible_error_text": data["visible_error_text"],
                       "horizontal_overflow": data["has_horizontal_overflow"],
                       "internal_links": data["internal_links"], "external_links": data["external_links"]})

        for st in cfg.get("interactive_states", []):
            if st["viewport"] != vp_name:
                continue
            pages = st.get("pages", "all")
            if pages != "all" and ref not in pages:
                continue
            try:
                if "click_any" in st:
                    sel, loc = first_visible(page, st["click_any"])
                    if not sel:
                        continue
                    loc.click(timeout=5000, no_wait_after=True)
                elif "hover_any" in st:
                    sel, loc = first_visible(page, st["hover_any"])
                    if not sel:
                        continue
                    loc.hover(timeout=5000)
                else:
                    continue
                page.wait_for_timeout(1000)
                if page.url.split("#")[0] != (result["final_url"] or "").split("#")[0]:
                    result["notes"].append(f"interactive state {st['name']} navigated away - not captured")
                    page.go_back(wait_until="load")
                    continue
                p = d["interactive"] / f"{base}_{st['name']}.png"
                page.screenshot(path=str(p), full_page=False)
                ev.add(ref, p, url, vp_name, f"interactive:{st['name']}", cfg)
                result["files"].append(str(p.relative_to(cfg["_out"])))
                result["notes"].append(f"interactive state {st['name']} via {sel}")
                page.keyboard.press("Escape")
                page.wait_for_timeout(300)
            except PWError as e:
                result["notes"].append(f"interactive state {st['name']} failed: {str(e)[:120]}")

        result["status"] = "captured" if (main_resp.get("status") or 0) < 400 else f"captured-http-{main_resp.get('status')}"
    except PWError as e:
        result["status"] = "failed"
        result["notes"].append(f"playwright error: {str(e)[:300]}")
    result["finished_at"] = utc_now()
    return result


def capture_url(pw_browser, row: dict, cfg: dict, d: dict, log: Log, ev: Evidence, asset_index: dict,
                save_assets: bool, vps: dict) -> tuple[dict, dict]:
    """ONE controlled browser navigation per URL. Headers, body, redirect chain are taken from that
    first response; all viewports are then captured by resizing the already-loaded page."""
    crawl = cfg["crawl"]
    url, ref, slug = row["normalized_url"], row["ref_id"], row["slug"]
    first_vp_name, first_vp = next(iter(vps.items()))
    ctx = pw_browser.new_context(viewport={"width": first_vp["width"], "height": first_vp["height"]},
                                 device_scale_factor=first_vp["device_scale_factor"],
                                 user_agent=cfg["user_agent"], locale="en-US",
                                 ignore_https_errors=False, java_script_enabled=True)
    page = ctx.new_page()
    page.set_default_timeout(crawl["page_timeout_ms"])
    main_resp: dict = {"navigation_started_at": utc_now()}
    seen_responses: list[dict] = []
    caps: dict = {}

    def on_response(resp):
        try:
            rurl = resp.url
            if not is_allowed(rurl, cfg):
                seen_responses.append({"url": rurl, "status": resp.status, "first_party": False})
                return
            ct = (resp.headers.get("content-type") or "").split(";")[0].strip()
            seen_responses.append({"url": rurl, "status": resp.status, "content_type": ct, "first_party": True})
            if not (save_assets and resp.ok and resp.request.resource_type != "document"):
                return
            n = normalize_url(rurl, cfg)
            if n in asset_index:
                return
            body = resp.body()
            if len(body) > cfg["asset_max_bytes"]:
                asset_index[n] = {"skipped": "too large", "bytes": len(body)}
                return
            bucket = asset_bucket(ct, rurl)
            dest = d[f"assets_{bucket}"] / asset_filename(rurl, ct)
            dest.write_bytes(body)
            asset_index[n] = {"path": str(dest.relative_to(cfg["_out"])), "content_type": ct,
                              "bytes": len(body), "status": resp.status, "first_seen_on": ref,
                              "captured_at": utc_now()}
            ev.add(ref, dest, rurl, "asset", bucket, cfg)
        except PWError:
            pass
        except Exception as e:  # never let asset saving break a capture
            main_resp.setdefault("asset_errors", []).append(repr(e))

    page.on("response", on_response)
    try:
        try:
            resp = page.goto(url, wait_until="load")
        except PWTimeout:
            main_resp["note"] = "load event timeout; continuing with partially loaded page"
            resp = None
        except PWError as e:
            main_resp["error"] = str(e)[:300]
            resp = None
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except PWTimeout:
            main_resp["networkidle"] = "not reached within 15s"
        page.wait_for_timeout(crawl["settle_wait_ms"])

        if resp is not None:
            chain = []
            req = resp.request
            while req.redirected_from is not None:
                prev = req.redirected_from
                pr = prev.response()
                chain.insert(0, {"url": prev.url, "status": pr.status if pr else None,
                                 "location": (pr.headers.get("location") if pr else None)})
                req = prev
            main_resp.update({"url": resp.url, "status": resp.status, "status_text": resp.status_text,
                              "headers": dict(resp.headers), "redirect_chain": chain,
                              "cache_headers": {k: v for k, v in resp.headers.items() if k.lower() in CACHE_HEADERS},
                              "cache_status": cache_status_from(resp.headers)})
            try:
                body = resp.body()
                main_resp["response_body_sha256"] = hashlib.sha256(body).hexdigest()
                main_resp["response_body_bytes"] = len(body)
                p = d["html_response"] / f"{ref}_{slug}.html"
                p.write_bytes(body)
                ev.add(ref, p, url, "single-request", "html_response", cfg)
                main_resp["response_body_file"] = str(p.relative_to(cfg["_out"]))
            except PWError as e:
                main_resp["response_body_error"] = str(e)[:200]
            write_json(d["headers"] / f"{ref}_{slug}.headers.json", {"ref_id": ref, "url": url, "captured_at": utc_now(), **main_resp})
            if chain:
                write_json(d["redirects"] / f"{ref}_{slug}.redirects.json", {"ref_id": ref, "url": url, "chain": chain, "final_url": resp.url})
        elif "error" in main_resp:
            write_json(d["headers"] / f"{ref}_{slug}.headers.json", {"ref_id": ref, "url": url, "captured_at": utc_now(), **main_resp})
            return main_resp, caps

        for vp_name, vp in vps.items():
            caps[vp_name] = capture_viewport(page, row, vp_name, vp, cfg, d, log, ev, main_resp, seen_responses)
    except PWError as e:
        main_resp["error"] = str(e)[:300]
    finally:
        try:
            ctx.close()
        except PWError:
            pass
    return main_resp, caps


def download_document(row: dict, cfg: dict, d: dict, ev: Evidence, log: Log) -> dict:
    url = row["normalized_url"]
    req = urllib.request.Request(url, headers={"User-Agent": cfg["user_agent"]})
    res = {"started_at": utc_now(), "status": "pending", "files": [], "notes": []}
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            headers = {k.lower(): v for k, v in resp.getheaders()}
            body = resp.read(cfg["asset_max_bytes"] + 1)
        record_request(row, "document-urllib", resp.status, cache_status_from(headers), "capture")
        if len(body) > cfg["asset_max_bytes"]:
            res["status"] = "skipped-too-large"; res["notes"].append("document exceeds asset_max_bytes")
            return res
        fn = f"{row['ref_id']}_{asset_filename(url, headers.get('content-type', ''))}"
        dest = d["assets_documents"] / fn
        dest.write_bytes(body)
        ev.add(row["ref_id"], dest, url, "document", "document", cfg)
        write_json(d["headers"] / f"{row['ref_id']}_{row['slug']}.headers.json",
                   {"ref_id": row["ref_id"], "url": url, "status": resp.status, "headers": headers,
                    "cache_headers": {k: v for k, v in headers.items() if k in CACHE_HEADERS}, "captured_at": utc_now()})
        res.update({"status": "captured", "http_status": resp.status, "bytes": len(body),
                    "files": [str(dest.relative_to(cfg["_out"]))], "content_type": headers.get("content-type")})
    except urllib.error.HTTPError as e:
        record_request(row, "document-urllib", e.code, cache_status_from(e.headers), "capture")
        res.update({"status": f"captured-http-{e.code}", "http_status": e.code})
        res["notes"].append(f"HTTP {e.code}")
    except Exception as e:
        res["status"] = "failed"; res["notes"].append(repr(e))
    res["finished_at"] = utc_now()
    return res


def environment_record(pw, browser, cfg: dict) -> dict:
    from importlib.metadata import version
    return {
        "recorded_at": utc_now(), "tool": TOOL_VERSION,
        "browser_name": browser.browser_type.name, "browser_version": browser.version,
        "playwright_version": version("playwright"), "python_version": platform.python_version(),
        "operating_environment": f"{platform.system()} {platform.release()} ({platform.machine()})",
        "user_agent": cfg["user_agent"], "viewports": cfg["viewports"], "crawl_settings": cfg["crawl"],
        "headless": True, "cache_bypass_headers_sent": False, "cache_busting_query_params": False,
        "authenticated": False, "one_navigation_per_url": True,
        "viewport_method": "single page load; tablet/mobile captured by in-page viewport resize (no reload)",
        "config_file": cfg["_config_path"],
    }


def _rollup(row: dict, caps: dict, main_resp: dict, cfg: dict) -> None:
    statuses = [caps.get(v, {}).get("status", "pending") for v in cfg["viewports"]]
    if statuses and all(s.startswith("captured") for s in statuses):
        row["capture_status"] = "captured" if all(s == "captured" for s in statuses) else "captured-with-http-error"
    elif any(s.startswith("captured") for s in statuses):
        row["capture_status"] = "partial"
    else:
        row["capture_status"] = "failed"
    first = next((caps[v] for v in cfg["viewports"] if caps.get(v, {}).get("title") is not None), None)
    if first:
        row["page_title"] = first["title"] or row.get("page_title", "")
        row["language"] = first.get("lang") or row.get("language") or "und"
        row["final_url"] = first.get("final_url") or row.get("final_url")
    if main_resp.get("status"):
        row["http_status"] = main_resp["status"]
        row["final_url"] = main_resp.get("url") or row.get("final_url")
        row["redirect_chain"] = [f"{c['url']} -> {c['status']} -> {c['location']}" for c in main_resp.get("redirect_chain", [])]
    row["screenshots"] = sorted({f for v in cfg["viewports"] for f in caps.get(v, {}).get("files", []) if f.endswith(".png")})


def run_capture(cfg: dict, refs: list[str] | None = None, viewports: list[str] | None = None,
                max_pages: int | None = None, force: bool = False, expand_links: bool = True) -> None:
    """Single-request capture crawl. New same-domain URLs found in each rendered page are appended to
    the manifest and captured in turn (BFS), so discovery and capture share the one request per URL."""
    if max_pages is None:
        max_pages = cfg["crawl"].get("max_pages_per_run")
    d = ensure_dirs(cfg)
    log = Log(cfg)
    ev = Evidence(cfg)
    crawl = cfg["crawl"]
    rows = load_manifest(cfg)
    if not rows:
        raise SystemExit("manifest is empty - run discovery first (robots.txt/sitemaps only)")
    idx = ManifestIndex(cfg, rows)
    vps = {k: v for k, v in cfg["viewports"].items() if not viewports or k in viewports}
    asset_idx_path = d["assets"] / "asset-index.json"
    asset_index = json.load(open(asset_idx_path)) if asset_idx_path.exists() else {}

    def needs_capture(r: dict) -> bool:
        if r["capture_status"] == "excluded" or r["content_type"] not in ("html", "document"):
            return False
        if refs and r["ref_id"] not in refs:
            return False
        return force or r["capture_status"] in ("pending", "partial", "failed")

    queue: deque[str] = deque(r["ref_id"] for r in rows if needs_capture(r))
    by_ref = {r["ref_id"]: r for r in rows}
    consecutive_5xx = 0
    requests_made = 0
    pages_done = 0
    halted = False
    log("INFO", f"capture run start: {len(queue)} rows queued, viewports={list(vps)}, one navigation per URL")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        env = environment_record(pw, browser, cfg)
        write_json(cfg["_out"] / "environment.json", env)
        log("INFO", f"browser {env['browser_name']} {env['browser_version']} playwright {env['playwright_version']}")
        try:
            while queue and not halted:
                if max_pages and pages_done >= max_pages:
                    log("STOP", f"max_pages {max_pages} reached; {len(queue)} rows still queued"); break
                if requests_made >= crawl["max_requests"]:
                    log("STOP", "max_requests reached"); break
                row = by_ref[queue.popleft()]
                if not needs_capture(row):
                    continue
                if row["content_type"] == "document":
                    res = download_document(row, cfg, d, ev, log)
                    row["captures"] = {"document": res}
                    row["http_status"] = res.get("http_status", row.get("http_status"))
                    row["final_url"] = row["final_url"] or row["normalized_url"]
                    row["capture_status"] = res["status"]
                    row["screenshots"] = []
                    requests_made += 1; pages_done += 1
                    log("INFO", f"document {row['ref_id']} {row['normalized_url']} -> {res['status']} cache={row.get('cache_status') or '-'}")
                    save_manifest(cfg, rows)
                    polite_sleep(crawl["delay_seconds"])
                    continue

                prior = row.get("request_count", 0)
                if prior:
                    row["notes"] = (row.get("notes", "") + f" {prior} prior request(s) before capture (see requests[])").strip()
                attempt, main_resp, caps = 0, {}, {}
                while attempt <= crawl["max_retries"]:
                    attempt += 1
                    main_resp, caps = capture_url(browser, row, cfg, d, log, ev, asset_index,
                                                  cfg.get("capture_assets", True), vps)
                    requests_made += 1
                    record_request(row, "browser-navigation", main_resp.get("status"), main_resp.get("cache_status"),
                                   "capture" if attempt == 1 else f"retry-{attempt - 1}")
                    status = main_resp.get("status") or 0
                    if status == 429:
                        log("STOP", f"HTTP 429 on {row['ref_id']} - halting capture"); halted = True; break
                    if status >= 500:
                        consecutive_5xx += 1
                        if consecutive_5xx >= crawl["stop_after_consecutive_5xx"]:
                            log("STOP", f"{consecutive_5xx} consecutive 5xx responses - halting"); halted = True; break
                    else:
                        consecutive_5xx = 0
                    # retry only when no response arrived at all (network/browser error); a served page - even a
                    # MISS or an unexpected design - is preserved as-is and never reloaded
                    if not status and attempt <= crawl["max_retries"]:
                        log("RETRY", f"{row['ref_id']} attempt {attempt} got no response: {main_resp.get('error', '')[:120]}; backing off")
                        polite_sleep(crawl["retry_backoff_seconds"] * attempt)
                        continue
                    break
                row["captures"] = caps
                row["main_response"] = {k: main_resp.get(k) for k in ("status", "url", "cache_status", "cache_headers",
                                                                       "response_body_sha256", "response_body_file", "error")}
                cache = main_resp.get("cache_status")
                row["cache_status"] = cache or row.get("cache_status") or ("none-disclosed" if status else "")
                if cache and cache.lower() != "hit":
                    row["notes"] = (row.get("notes", "") + f" cache {cache.upper()} on capture - served uncached/regenerated at capture time; flagged, not reloaded").strip()
                    log("FLAG", f"{row['ref_id']} cache={cache} {row['normalized_url']}")
                _rollup(row, caps, main_resp, cfg)
                pages_done += 1
                log("INFO", f"{row['ref_id']} {row['normalized_url']} -> {row['capture_status']} http={status} cache={cache or '-'} "
                            f"h={'/'.join(str(caps.get(v, {}).get('page_height', '-')) for v in vps)}")

                if expand_links and caps:
                    links = set()
                    for v in caps.values():
                        links.update(v.get("internal_links") or [])
                    added = 0
                    for u in sorted(links):
                        if not is_allowed(u, cfg):
                            continue
                        child = idx.entry(u, f"link:{row['ref_id']}")
                        if child is row:
                            continue
                        if row["ref_id"] not in child["linked_from"]:
                            child["linked_from"].append(row["ref_id"])
                        if child["ref_id"] not in by_ref:
                            by_ref[child["ref_id"]] = child
                            if needs_capture(child):
                                queue.append(child["ref_id"]); added += 1
                    if main_resp.get("url") and is_allowed(main_resp["url"], cfg) and normalize_url(main_resp["url"], cfg) != row["normalized_url"]:
                        tgt = idx.entry(main_resp["url"], f"redirect:{row['normalized_url']}")
                        row["notes"] = (row.get("notes", "") + f" redirects to {tgt['ref_id']}").strip()
                        if tgt["ref_id"] not in by_ref:
                            by_ref[tgt["ref_id"]] = tgt
                            if needs_capture(tgt):
                                queue.append(tgt["ref_id"]); added += 1
                    write_json(d["links"] / f"{row['ref_id']}_{row['slug']}.links.json",
                               {"ref_id": row["ref_id"], "url": row["normalized_url"], "captured_at": utc_now(),
                                "internal": sorted(links),
                                "external": sorted({e for v in caps.values() for e in (v.get("external_links") or [])})})
                    if added:
                        log("INFO", f"{row['ref_id']}: {added} new URL(s) queued; queue={len(queue)} manifest={len(rows)}")
                for v in caps.values():
                    v.pop("internal_links", None); v.pop("external_links", None)
                save_manifest(cfg, rows)
                write_json(asset_idx_path, asset_index)
                polite_sleep(crawl["delay_seconds"])
        finally:
            browser.close()
            write_json(asset_idx_path, asset_index)
            save_manifest(cfg, rows)
    log("INFO", f"capture run end: pages={pages_done} requests={requests_made} halted={halted} queued={len(queue)}")
