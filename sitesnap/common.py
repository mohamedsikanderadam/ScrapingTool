"""Shared helpers: configuration, URL normalisation, logging, manifest persistence."""
from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, quote, unquote, urlencode, urljoin, urlsplit, urlunsplit

PKG_ROOT = Path(__file__).resolve().parent
TOOL_VERSION = "sitesnap 1.0.0"
DEFAULT_CONFIG = PKG_ROOT / "default.config.json"

MANIFEST_FIELDS = [
    "ref_id", "discovered_url", "normalized_url", "final_url", "discovery_source",
    "page_title", "language", "content_type", "http_status", "redirect_chain",
    "capture_status", "cache_status", "request_count", "screenshots", "notes",
]


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_config(path: str | Path | None = None) -> dict:
    p = Path(path) if path else DEFAULT_CONFIG
    with open(p, encoding="utf-8") as fh:
        cfg = json.load(fh)
    return finalize_config(cfg, p)


def finalize_config(cfg: dict, source: str | Path = "<generated>") -> dict:
    cfg["_config_path"] = str(source)
    out = Path(cfg["output_dir"]).expanduser()
    cfg["_out"] = out if out.is_absolute() else Path.cwd() / out
    return cfg


def config_for_url(url: str, output_dir: str | Path | None = None, *, name: str | None = None,
                   max_pages: int | None = None, delay_seconds: float | None = None,
                   viewports: list[str] | None = None, capture_assets: bool | None = None,
                   extra_hosts: list[str] | None = None) -> dict:
    """Build a full capture configuration from just a site URL, starting from default.config.json."""
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    parts = urlsplit(url)
    host = parts.hostname or ""
    if not host:
        raise ValueError(f"not a valid URL: {url}")
    bare = host[4:] if host.startswith("www.") else host
    origin = f"{parts.scheme}://{parts.netloc}"
    with open(DEFAULT_CONFIG, encoding="utf-8") as fh:
        cfg = json.load(fh)
    cfg["project"] = f"{name or bare} - site snapshot"
    cfg["client"] = name or bare
    cfg["base_url"] = origin + "/"
    cfg["allowed_hosts"] = sorted({host, bare, "www." + bare, *(extra_hosts or [])})
    cfg["seeds"] = [origin + "/"] + ([url] if parts.path not in ("", "/") else []) + [
        origin + "/sitemap.xml", origin + "/sitemap_index.xml", origin + "/robots.txt"]
    cfg["output_dir"] = str(output_dir or Path("snapshots") / f"{bare}-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}")
    if max_pages is not None:
        cfg["crawl"]["max_pages_per_run"] = max_pages
    if delay_seconds is not None:
        cfg["crawl"]["delay_seconds"] = delay_seconds
    if viewports:
        cfg["viewports"] = {k: v for k, v in cfg["viewports"].items() if k in viewports}
    if capture_assets is not None:
        cfg["capture_assets"] = capture_assets
    return finalize_config(cfg)


def save_config(cfg: dict) -> Path:
    out: Path = cfg["_out"]
    out.mkdir(parents=True, exist_ok=True)
    p = out / "capture.config.json"
    json.dump({k: v for k, v in cfg.items() if not k.startswith("_")}, open(p, "w", encoding="utf-8"), indent=2)
    return p


def out_dirs(cfg: dict) -> dict[str, Path]:
    o = cfg["_out"]
    d = {
        "root": o,
        "shots": o / "screenshots",
        "interactive": o / "screenshots" / "interactive-states",
        "html_response": o / "html" / "response",
        "html_rendered": o / "html" / "rendered",
        "assets": o / "assets",
        "headers": o / "metadata" / "headers",
        "redirects": o / "metadata" / "redirects",
        "links": o / "metadata" / "links",
        "page_data": o / "metadata" / "page-data",
        "contact": o / "reports" / "visual-contact-sheets",
        "broken": o / "reports" / "broken-pages",
        "coverage": o / "reports" / "crawl-coverage",
        "integrity": o / "integrity",
        "state": o / ".state",
    }
    for vp in cfg["viewports"]:
        d[f"shots_{vp}_full"] = o / "screenshots" / vp / "full-page"
        d[f"shots_{vp}_fold"] = o / "screenshots" / vp / "above-fold"
    for sub in ("images", "styles", "scripts", "fonts", "documents", "other"):
        d[f"assets_{sub}"] = o / "assets" / sub
    return d


def ensure_dirs(cfg: dict) -> dict[str, Path]:
    d = out_dirs(cfg)
    for p in d.values():
        p.mkdir(parents=True, exist_ok=True)
    return d


class Log:
    """Append-only capture log (integrity/capture-log.txt) that also echoes to stdout."""

    def __init__(self, cfg: dict):
        self.path = out_dirs(cfg)["integrity"] / "capture-log.txt"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def __call__(self, level: str, msg: str) -> None:
        line = f"{utc_now()} [{level}] {msg}"
        print(line, flush=True)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")


# ---------------------------------------------------------------- URL helpers

def host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def is_allowed(url: str, cfg: dict) -> bool:
    return host_of(url) in {h.lower() for h in cfg["allowed_hosts"]}


def normalize_url(url: str, cfg: dict, base: str | None = None) -> str:
    """Resolve relative links, drop fragments and tracking params, canonicalise host/scheme."""
    if base:
        url = urljoin(base, url)
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    scheme = parts.scheme
    if scheme in ("http", "https") and host in {h.lower() for h in cfg["allowed_hosts"]}:
        scheme = urlsplit(cfg["base_url"]).scheme or "https"
    allowed = {h.lower() for h in cfg["allowed_hosts"]}
    base_host = host_of(cfg["base_url"])
    if host in allowed and host != base_host:
        # canonicalise www./bare variants to whatever the seed URL uses
        if host.startswith("www.") and host[4:] == base_host:
            host = base_host
        elif "www." + host == base_host:
            host = base_host
    path = parts.path or "/"
    # keep unicode paths readable but consistently percent-encoded
    path = quote(unquote(path), safe="/%:@!$&'()*+,;=-._~")
    tracking = set(cfg.get("tracking_params", []))
    q = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k not in tracking]
    query = urlencode(sorted(q), doseq=True)
    return urlunsplit((scheme, host, path, query, ""))


def is_excluded(url: str, cfg: dict) -> bool:
    parts = urlsplit(url)
    target = parts.path + ("?" + parts.query if parts.query else "")
    if any(re.search(pat, target) for pat in cfg["exclude_path_patterns"]):
        return True
    return excluded_reason_for(url, cfg) is not None


def excluded_reason_for(url: str, cfg: dict) -> str | None:
    """Faceted-filter combinations (2+ filter_* params, or 2+ comma-separated values in one) are uncached,
    combinatorial and would each regenerate a page; only single-facet, single-value filter URLs are captured."""
    parts = urlsplit(url)
    max_facets = cfg.get("max_filter_params", 1)
    facets = [v for k, v in parse_qsl(parts.query, keep_blank_values=True) if k.startswith("filter_")]
    if len(facets) > max_facets or any("," in v for v in facets):
        return cfg.get("excluded_filter_reason", "Combinatorial faceted-filter URL not captured")
    return None


def classify(url: str, content_type_header: str | None) -> str:
    path = urlsplit(url).path.lower()
    ct = (content_type_header or "").lower()
    if any(path.endswith(e) for e in (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".zip")) or "application/pdf" in ct:
        return "document"
    if path.endswith((".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico")) or ct.startswith("image/"):
        return "image"
    if path.endswith(".xml") or "xml" in ct and "html" not in ct:
        return "xml"
    if "text/html" in ct or ct == "" or path.endswith((".html", "/")) or "." not in path.rsplit("/", 1)[-1]:
        return "html"
    return "other"


def slugify(url: str, max_len: int = 60) -> str:
    parts = urlsplit(url)
    raw = unquote(parts.path).strip("/") or "home"
    if parts.query:
        raw += "_" + parts.query
    raw = unicodedata.normalize("NFKD", raw)
    raw = re.sub(r"[^\w\-]+", "_", raw, flags=re.UNICODE).strip("_").lower()
    raw = re.sub(r"_+", "_", raw)
    return raw[:max_len] or "page"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def short_hash(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:10]


# ------------------------------------------------------------ manifest I/O

def manifest_paths(cfg: dict) -> tuple[Path, Path]:
    o = cfg["_out"]
    return o / "url-manifest.json", o / "url-manifest.csv"


def load_manifest(cfg: dict) -> list[dict]:
    jp, _ = manifest_paths(cfg)
    if jp.exists():
        with open(jp, encoding="utf-8") as fh:
            return json.load(fh)
    return []


def save_manifest(cfg: dict, rows: list[dict]) -> None:
    jp, cp = manifest_paths(cfg)
    jp.parent.mkdir(parents=True, exist_ok=True)
    tmp = jp.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(rows, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, jp)
    with open(cp, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=MANIFEST_FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            rr = dict(r)
            for k in ("redirect_chain", "screenshots", "discovery_source"):
                v = rr.get(k)
                if isinstance(v, list):
                    rr[k] = " | ".join(str(x) for x in v)
            w.writerow(rr)


class ManifestIndex:
    """Manifest rows keyed by normalized URL; assigns stable P#### IDs to new URLs."""

    def __init__(self, cfg: dict, rows: list[dict]):
        self.cfg = cfg
        self.rows = rows
        self.by_norm = {r["normalized_url"]: r for r in rows}
        self.next_id = max([int(r["ref_id"][1:]) for r in rows] + [0]) + 1

    def entry(self, url: str, source: str) -> dict:
        n = normalize_url(url, self.cfg)
        row = self.by_norm.get(n)
        if row:
            if source not in row["discovery_source"]:
                row["discovery_source"].append(source)
            return row
        row = {
            "ref_id": f"P{self.next_id:04d}", "discovered_url": url, "normalized_url": n,
            "final_url": "", "discovery_source": [source], "page_title": "", "language": "",
            "content_type": classify(n, None), "http_status": None, "redirect_chain": [],
            "capture_status": "pending", "cache_status": "", "request_count": 0,
            "screenshots": [], "notes": "", "slug": slugify(n), "discovered_at": utc_now(),
            "in_sitemap": False, "linked_from": [], "requests": [],
        }
        if is_excluded(n, self.cfg):
            row["capture_status"] = "excluded"
            row["notes"] = excluded_reason_for(n, self.cfg) or self.cfg["excluded_reason"]
        self.next_id += 1
        self.by_norm[n] = row
        self.rows.append(row)
        return row


CACHE_STATUS_HEADERS = ["x-litespeed-cache", "cf-cache-status", "x-cache", "x-cache-status", "x-vercel-cache",
                        "x-nf-request-id", "x-varnish-cache", "x-proxy-cache", "x-fastly-cache", "x-sucuri-cache",
                        "x-nginx-cache", "x-cache-enabled", "x-served-by"]


def cache_status_from(headers) -> str | None:
    """Normalised HIT/MISS/... from whatever cache-status header the site discloses (any CDN/plugin)."""
    if not headers:
        return None
    get = headers.get if hasattr(headers, "get") else (lambda k: None)
    for h in CACHE_STATUS_HEADERS:
        v = get(h)
        if v:
            m = re.search(r"\b(hit|miss|bypass|expired|stale|dynamic|revalidated|updating|no-cache)\b", str(v), re.I)
            return m.group(1).lower() if m else str(v).split(",")[0].strip()
    return None


def record_request(row: dict, via: str, status, cache: str | None, purpose: str) -> None:
    """Every request against a URL is logged on its manifest row (a cache MISS may have regenerated the page)."""
    row.setdefault("requests", []).append({"at": utc_now(), "via": via, "status": status,
                                            "cache_status": cache, "purpose": purpose})
    row["request_count"] = len(row["requests"])
    if cache and not row.get("cache_status"):
        row["cache_status"] = cache


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False, default=str)


def polite_sleep(seconds: float) -> None:
    if seconds > 0:
        time.sleep(seconds)


def die(msg: str, code: int = 2) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(code)
