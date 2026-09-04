# sitesnap

Paste a URL, get a complete visual snapshot of the public website: every page screenshotted on
**desktop (1440×900), tablet (768×1024) and mobile (390×844)**, plus the HTML, HTTP headers, redirects,
page metadata, a review gallery and a PDF report with all screenshots embedded.

It is the generalised version of the crawler built for the Emirates Paints visual recovery reference.

## What it does

1. **Discover** – reads `robots.txt` and XML sitemaps, then follows same-site links found in each rendered page.
2. **Capture** – opens each page **once** in headless Chromium, records the response headers/body, scrolls to trigger
   lazy loading and saves above-the-fold + full-page PNGs for each viewport (tablet/mobile via in-page resize, so no
   extra requests). Safe interactive states (mobile menu, search, nav hover, cookie banner) are captured where present.
3. **QA** – flags blank/duplicate screenshots, broken images, error text and layout collapse.
4. **Report** – `summary.pdf`, `capture-summary.md`, `cache-observations.md`, `exceptions.md`, an HTML gallery and
   `full-report-with-screenshots.pdf` (split into parts for very large sites).

Safety defaults: one request at a time with a delay, no logins, no form submissions, admin/account/cart/checkout/search/
feed/API URLs excluded, third-party hosts never crawled, hard stop on HTTP 429 or repeated 5xx. Every request made to
the site is logged per URL in `url-manifest.json`.

## Install

```bash
git clone https://github.com/mohamedsikanderadam/ScrapingTool.git
cd ScrapingTool
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -e .
python -m playwright install chromium
```

Python 3.10+.

## Use

### Command line

```bash
sitesnap https://example.com
```

Output goes to `snapshots/example.com-<timestamp>/`. Useful options:

```bash
sitesnap https://example.com --max-pages 50          # default 200
sitesnap https://example.com --viewports desktop mobile
sitesnap https://example.com --delay 3               # seconds between requests (default 1.5)
sitesnap https://example.com --assets                # also archive first-party CSS/JS/images/fonts
sitesnap https://example.com --no-pdf                # skip the screenshot PDF book
sitesnap https://example.com -o my-folder --name "Client name"
```

`python -m sitesnap ...` works too if the `sitesnap` script is not on your PATH.

### Web UI

```bash
sitesnap serve            # http://127.0.0.1:8765
```

Paste a URL, press **Snapshot**, watch progress (pages / screenshots), then open the gallery, the PDFs or download the
whole snapshot as a zip. Several snapshots can be queued; they run one after another in the background.

### Resume or re-run stages

Each snapshot folder contains the exact `capture.config.json` used. Capture is restartable:

```bash
sitesnap --config snapshots/example.com-.../capture.config.json capture    # continues where it stopped
sitesnap --config snapshots/example.com-.../capture.config.json report
sitesnap --config snapshots/example.com-.../capture.config.json pdfbook
```

Edit the config to add site-specific menu/search selectors under `interactive_states`, extra hosts under
`allowed_hosts`, or more `exclude_path_patterns`.

## Output layout

```
snapshots/<site>-<timestamp>/
  summary.pdf                          short report
  full-report-with-screenshots.pdf     report + every screenshot (parts for big sites)
  capture-summary.md                   full report (Markdown)
  cache-observations.md, exceptions.md
  url-manifest.csv / .json             every URL: ref id, status, redirects, cache status, requests made
  screenshots/{desktop,tablet,mobile}/{full-page,above-fold}/P0001_home_en_desktop_full.png
  screenshots/interactive-states/
  html/response/, html/rendered/       original HTML and rendered DOM per viewport
  metadata/{headers,redirects,links,page-data}/
  reports/visual-contact-sheets/index.html   review gallery
  reports/crawl-coverage/              sitemap/robots copies, QA findings
  integrity/SHA256SUMS.txt             hash of every file
  capture.config.json, status.json, environment.json
```

## Notes

- Only publicly reachable pages are captured, as an anonymous first-time visitor sees them.
- Full-page screenshots taller than 16,000 px are clipped (noted in the manifest).
- Sites behind bot protection (Cloudflare challenges etc.) may return challenge pages; these are captured and flagged
  as-is rather than retried.
- Be considerate: keep the default delay for sites you do not own, and get permission before snapshotting large sites.
