"""Report writers + run comparison for the benchmark harness.

The runner (benchmarks.runner) owns execution; this module owns presentation
and cross-run comparison. Kept import-light so compare can be used standalone.
"""

from __future__ import annotations

import json
from pathlib import Path


def load_metrics(run_dir: str | Path, repo_root: Path | None = None) -> dict:
    root = repo_root or Path(__file__).resolve().parent.parent
    metrics: dict[str, dict] = {}
    base = root / run_dir if not Path(run_dir).is_absolute() else Path(run_dir)
    for adapter_dir in sorted(p for p in base.iterdir() if p.is_dir()):
        mf = adapter_dir / "metrics.json"
        if mf.exists():
            metrics[adapter_dir.name] = json.loads(mf.read_text(encoding="utf-8"))
    return metrics


def _flatten(d: dict, prefix: str = "") -> dict[str, float]:
    out: dict[str, float] = {}
    for key, val in d.items():
        if isinstance(val, dict):
            out.update(_flatten(val, prefix + key + "."))
        elif isinstance(val, (int, float)):
            out[prefix + key] = float(val)
    return out


def compare(old_dir: str | Path, new_dir: str | Path) -> int:
    """Print per-metric deltas between two artifact runs. Returns 1 on any
    regression, else 0."""
    old_metrics = load_metrics(old_dir)
    new_metrics = load_metrics(new_dir)
    exit_code = 0
    for adapter in sorted(set(old_metrics) & set(new_metrics)):
        o, n = _flatten(old_metrics[adapter]), _flatten(new_metrics[adapter])
        print(f"\n== {adapter} ==")
        for key in sorted(set(o) | set(n)):
            ov, nv = o.get(key), n.get(key)
            if ov is None or nv is None:
                print(f"  {key:36s} {ov} → {nv} (added/removed)")
                continue
            delta = nv - ov
            marker = ""
            if abs(delta) > 1e-9:
                marker = "REGRESSION" if delta < 0 else "improved"
                if delta < 0:
                    exit_code = 1
            print(f"  {key:36s} {ov:.4f} → {nv:.4f}  Δ{delta:+.4f} {marker}")
    return exit_code
