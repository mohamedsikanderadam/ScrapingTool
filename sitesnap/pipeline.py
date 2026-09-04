"""One-call pipeline: URL -> discover -> capture -> qa -> integrity -> report (-> pdfbook)."""
from __future__ import annotations

import json
from pathlib import Path

from .common import Log, config_for_url, save_config, utc_now

STAGES = ["discover", "capture", "qa", "integrity", "report", "pdfbook"]


def _status(cfg: dict, **fields) -> None:
    p: Path = cfg["_out"] / "status.json"
    cur = json.load(open(p)) if p.exists() else {}
    cur.update(fields, updated_at=utc_now())
    p.parent.mkdir(parents=True, exist_ok=True)
    json.dump(cur, open(p, "w"), indent=2)


def snapshot(url: str, output_dir: str | Path | None = None, *, name: str | None = None, max_pages: int | None = None,
             delay_seconds: float | None = None, viewports: list[str] | None = None, capture_assets: bool | None = None,
             pdf: bool = True, cfg: dict | None = None) -> dict:
    """Run the whole pipeline for a site URL. Returns the finalised config (cfg['_out'] is the output folder)."""
    cfg = cfg or config_for_url(url, output_dir, name=name, max_pages=max_pages, delay_seconds=delay_seconds,
                                viewports=viewports, capture_assets=capture_assets)
    save_config(cfg)
    log = Log(cfg)
    log("INFO", f"sitesnap start url={url} out={cfg['_out']}")
    _status(cfg, url=url, state="running", stage="discover", started_at=utc_now(), error=None)
    try:
        from .discover import run_discovery
        from .capture import run_capture
        from .qa import run_qa
        from .integrity import run_integrity
        from .report import run_report

        run_discovery(cfg)
        _status(cfg, stage="capture")
        run_capture(cfg)
        _status(cfg, stage="qa")
        run_qa(cfg)
        _status(cfg, stage="integrity")
        run_integrity(cfg)
        _status(cfg, stage="report")
        run_report(cfg)
        if pdf:
            _status(cfg, stage="pdfbook")
            from .pdfbook import run_pdfbook
            run_pdfbook(cfg)
        _status(cfg, state="done", stage="done", finished_at=utc_now())
        log("INFO", "sitesnap finished")
    except BaseException as e:  # noqa: BLE001 - record the failure, then re-raise
        _status(cfg, state="failed", error=f"{type(e).__name__}: {e}", finished_at=utc_now())
        log("ERROR", f"sitesnap failed: {type(e).__name__}: {e}")
        raise
    return cfg
