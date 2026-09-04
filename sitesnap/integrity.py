"""SHA-256 hashing of every evidence file, evidence register and SHA256SUMS.txt."""
from __future__ import annotations

import csv
import json
from pathlib import Path

from .common import Log, TOOL_VERSION, ensure_dirs, sha256_file, utc_now, write_json

EVIDENCE_DIRS = ("screenshots", "html", "assets", "metadata")
REGISTER_FIELDS = ["evidence_ref_id", "filename", "relative_path", "sha256", "size_bytes",
                   "captured_at_utc", "source_url", "capture_profile", "kind", "tool_version"]


def run_integrity(cfg: dict) -> dict:
    d = ensure_dirs(cfg)
    log = Log(cfg)
    out: Path = cfg["_out"]
    ledger: dict[str, dict] = {}
    lp = d["state"] / "evidence.jsonl"
    if lp.exists():
        for line in open(lp, encoding="utf-8"):
            rec = json.loads(line)
            ledger[rec["relative_path"]] = rec  # last write wins (re-captures)

    reg_path = out / "evidence-register.csv"
    existing: dict[str, dict] = {}
    if reg_path.exists():
        with open(reg_path, encoding="utf-8", newline="") as fh:
            for r in csv.DictReader(fh):
                existing[r["relative_path"]] = r

    rows: list[dict] = []
    changed, added = 0, 0
    files = sorted(p for sub in EVIDENCE_DIRS for p in (out / sub).rglob("*") if p.is_file())
    for p in files:
        rel = str(p.relative_to(out))
        digest = sha256_file(p)
        meta = ledger.get(rel, {})
        prev = existing.get(rel)
        if prev and prev["sha256"] != digest:
            changed += 1
            log("WARN", f"hash changed since last register: {rel} (old kept in capture log)")
        elif not prev:
            added += 1
        rows.append({
            "evidence_ref_id": meta.get("ref_id", (p.name.split("_")[0] if p.name.startswith("P") else "ASSET")),
            "filename": p.name, "relative_path": rel, "sha256": digest, "size_bytes": p.stat().st_size,
            "captured_at_utc": meta.get("captured_at", prev["captured_at_utc"] if prev else utc_now()),
            "source_url": meta.get("source_url", prev["source_url"] if prev else ""),
            "capture_profile": meta.get("capture_profile", prev["capture_profile"] if prev else ""),
            "kind": meta.get("kind", prev["kind"] if prev else ""),
            "tool_version": meta.get("tool", TOOL_VERSION),
        })
    with open(reg_path, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=REGISTER_FIELDS)
        w.writeheader(); w.writerows(rows)
    with open(d["integrity"] / "SHA256SUMS.txt", "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(f"{r['sha256']}  {r['relative_path']}\n")
    # also hash the manifests/reports themselves so the package is self-verifying
    top = [out / n for n in ("url-manifest.json", "url-manifest.csv", "evidence-register.csv", "environment.json")]
    with open(d["integrity"] / "SHA256SUMS-manifests.txt", "w", encoding="utf-8") as fh:
        for p in top:
            if p.exists():
                fh.write(f"{sha256_file(p)}  {p.relative_to(out)}\n")
    summary = {"generated_at": utc_now(), "files_hashed": len(rows), "added": added, "changed": changed,
               "total_bytes": sum(r["size_bytes"] for r in rows)}
    write_json(d["integrity"] / "integrity-summary.json", summary)
    log("INFO", f"integrity: {len(rows)} files hashed, {added} new, {changed} changed, {summary['total_bytes']/1e6:.1f} MB")
    return summary
