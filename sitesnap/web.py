"""Minimal local web UI (standard library only): paste a URL, watch progress, browse/download the results.

    python -m sitesnap serve  ->  http://127.0.0.1:8765
"""
from __future__ import annotations

import html
import json
import shutil
import threading
import traceback
import zipfile
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .common import config_for_url, finalize_config, utc_now
from .pipeline import snapshot

_jobs: dict[str, dict] = {}
_lock = threading.Lock()

PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>sitesnap</title>
<style>
body{font-family:system-ui,sans-serif;max-width:900px;margin:40px auto;padding:0 16px;color:#222}
h1{font-weight:600}form{display:flex;gap:8px;flex-wrap:wrap;margin:20px 0}
input[type=url]{flex:1;min-width:280px;padding:10px;font-size:15px;border:1px solid #bbb;border-radius:6px}
input[type=number]{width:90px;padding:10px;border:1px solid #bbb;border-radius:6px}
button{padding:10px 18px;font-size:15px;background:#1a56db;color:#fff;border:0;border-radius:6px;cursor:pointer}
table{border-collapse:collapse;width:100%%}td,th{border-bottom:1px solid #eee;padding:8px;text-align:left;font-size:14px;vertical-align:top}
.state-running{color:#b45309}.state-done{color:#047857}.state-failed{color:#b91c1c}
small{color:#666}pre{background:#f6f6f6;padding:8px;font-size:12px;max-height:240px;overflow:auto}
</style></head><body>
<h1>sitesnap</h1>
<p>Paste a website URL. Every public page is discovered (sitemap, robots.txt, internal links) and screenshotted on
desktop, tablet and mobile, with HTML, headers and a PDF report. Nothing is submitted or changed on the site.</p>
<form method="post" action="/start">
 <input type="url" name="url" placeholder="https://example.com" required autofocus>
 <input type="number" name="max_pages" value="200" min="1" title="max pages">
 <label><input type="checkbox" name="pdf" checked> PDF book</label>
 <button type="submit">Snapshot</button>
</form>
<h2>Snapshots</h2>
<div id="jobs">%(jobs)s</div>
<script>setInterval(()=>fetch('/jobs').then(r=>r.text()).then(t=>{document.getElementById('jobs').innerHTML=t}),3000)</script>
</body></html>"""


def _fmt_jobs(root: Path) -> str:
    rows = []
    with _lock:
        jobs = sorted(_jobs.values(), key=lambda j: j["started_at"], reverse=True)
    for j in jobs:
        out: Path = j["out"]
        st = {}
        sp = out / "status.json"
        if sp.exists():
            try:
                st = json.load(open(sp))
            except json.JSONDecodeError:
                pass
        state = st.get("state") or j["state"]
        stage = st.get("stage", "")
        n_shots = sum(1 for _ in (out / "screenshots").rglob("*.png")) if (out / "screenshots").exists() else 0
        n_pages = 0
        mp = out / "url-manifest.json"
        if mp.exists():
            try:
                n_pages = sum(1 for r in json.load(open(mp)) if str(r.get("capture_status", "")).startswith("captured"))
            except (json.JSONDecodeError, TypeError):
                pass
        rel = out.relative_to(root).as_posix()
        links = [f'<a href="/files/{rel}/" target="_blank">browse</a>']
        if (out / "reports/visual-contact-sheets/index.html").exists():
            links.append(f'<a href="/files/{rel}/reports/visual-contact-sheets/index.html" target="_blank">gallery</a>')
        if (out / "summary.pdf").exists():
            links.append(f'<a href="/files/{rel}/summary.pdf" target="_blank">summary.pdf</a>')
        for pdf in sorted(out.glob("full-report-with-screenshots*.pdf")):
            links.append(f'<a href="/files/{rel}/{pdf.name}" target="_blank">{html.escape(pdf.name)}</a>')
        if state in ("done", "failed"):
            links.append(f'<a href="/zip/{rel}">download zip</a>')
        err = f"<pre>{html.escape(st.get('error') or j.get('error') or '')}</pre>" if (st.get("error") or j.get("error")) else ""
        rows.append(f"<tr><td><b>{html.escape(j['url'])}</b><br><small>{html.escape(rel)}<br>started {j['started_at']}</small>{err}</td>"
                    f"<td class='state-{state}'>{state}<br><small>{stage}</small></td>"
                    f"<td>{n_pages} pages<br>{n_shots} screenshots</td><td>{' · '.join(links)}</td></tr>")
    if not rows:
        return "<p><small>No snapshots yet.</small></p>"
    return "<table><tr><th>Site</th><th>State</th><th>Progress</th><th>Results</th></tr>" + "".join(rows) + "</table>"


def _run_job(job: dict) -> None:
    try:
        snapshot(job["url"], cfg=job["cfg"], pdf=job["pdf"])
        job["state"] = "done"
    except Exception as e:  # noqa: BLE001
        job["state"] = "failed"
        job["error"] = f"{type(e).__name__}: {e}\n{traceback.format_exc()[-1500:]}"


class Handler(SimpleHTTPRequestHandler):
    root: Path

    def __init__(self, *a, root: Path, **kw):
        self.root = root
        super().__init__(*a, directory=str(root), **kw)

    def log_message(self, fmt, *args):  # quieter
        pass

    def _send(self, body: str, code: int = 200, ctype: str = "text/html; charset=utf-8") -> None:
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/":
            return self._send(PAGE % {"jobs": _fmt_jobs(self.root)})
        if path == "/jobs":
            return self._send(_fmt_jobs(self.root))
        if path.startswith("/zip/"):
            rel = path[len("/zip/"):].strip("/")
            target = (self.root / rel).resolve()
            if not target.is_dir() or self.root.resolve() not in target.parents:
                return self._send("not found", 404, "text/plain")
            zpath = self.root / f"{target.name}.zip"
            if not zpath.exists() or zpath.stat().st_mtime < max(p.stat().st_mtime for p in target.rglob("*") if p.is_file()):
                with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as zf:
                    for p in target.rglob("*"):
                        if p.is_file():
                            zf.write(p, p.relative_to(target.parent))
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", f'attachment; filename="{zpath.name}"')
            self.send_header("Content-Length", str(zpath.stat().st_size))
            self.end_headers()
            with open(zpath, "rb") as fh:
                shutil.copyfileobj(fh, self.wfile)
            return
        if path.startswith("/files/"):
            self.path = self.path[len("/files"):]
            return super().do_GET()
        return self._send("not found", 404, "text/plain")

    def do_POST(self):
        if urlsplit(self.path).path != "/start":
            return self._send("not found", 404, "text/plain")
        n = int(self.headers.get("Content-Length", "0"))
        form = parse_qs(self.rfile.read(n).decode("utf-8"))
        url = (form.get("url") or [""])[0].strip()
        try:
            max_pages = int((form.get("max_pages") or ["200"])[0])
            cfg = config_for_url(url, max_pages=max_pages)
        except ValueError as e:
            return self._send(f"<p>Invalid input: {html.escape(str(e))}</p><a href='/'>back</a>", 400)
        # place output under the served root
        cfg["output_dir"] = str(self.root / Path(cfg["output_dir"]).name)
        finalize_config(cfg)
        job = {"url": url, "cfg": cfg, "out": cfg["_out"], "pdf": bool(form.get("pdf")), "state": "running",
               "started_at": utc_now(), "error": None}
        with _lock:
            _jobs[str(cfg["_out"])] = job
        threading.Thread(target=_run_job, args=(job,), daemon=True).start()
        self.send_response(303)
        self.send_header("Location", "/")
        self.end_headers()


def _load_existing(root: Path) -> None:
    """Show snapshots from previous runs (found via their status.json)."""
    for sp in root.glob("*/status.json"):
        try:
            st = json.load(open(sp))
        except json.JSONDecodeError:
            continue
        out = sp.parent
        _jobs[str(out)] = {"url": st.get("url", out.name), "cfg": None, "out": out, "pdf": True,
                           "state": "interrupted" if st.get("state") == "running" else st.get("state", "done"),
                           "started_at": (st.get("started_at") or "").replace("T", " "), "error": st.get("error")}


def serve(host: str = "127.0.0.1", port: int = 8765, root: str | Path = "snapshots") -> None:
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    _load_existing(root)
    srv = ThreadingHTTPServer((host, port), partial(Handler, root=root))
    print(f"sitesnap web UI: http://{host}:{port}/   (snapshots -> {root})", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
