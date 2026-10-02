"""Human-facing renderings of run reports: terminal tree and standalone HTML."""

from __future__ import annotations

import html
import json
from typing import Any

_STATUS = {"completed": "ok", "failed": "FAIL", "skipped": "skip", "waiting": "WAIT",
           "running": "run", "pending": "...", "ok": "ok", "error": "FAIL",
           "interrupted": "INTR"}


def render_text(report: dict[str, Any], *, color: bool = False) -> str:
    def paint(text: str, code: str) -> str:
        return f"\033[{code}m{text}\033[0m" if color else text

    lines = [
        f"run {report['run_id']}  [{report['status']}]  {report.get('goal', '')}",
        f"  agent={report.get('agent')}  plan label: {report['plan_label']}",
    ]
    usage = report["usage"]
    cost = f"${usage['cost_usd']:.6f}" + (f" (+{usage['unpriced_calls']} unpriced)" if usage["unpriced_calls"] else "")
    lines.append(
        f"  tokens={usage['tokens']} cost={cost} model_calls={usage['model_calls']} "
        f"tool_calls={usage['tool_calls']} replayed={usage['replayed_effects']} "
        f"duration={report.get('duration_s')}s"
    )
    lines.append("")
    trace = report["trace"]
    for node_span in trace["children"]:
        status = _STATUS.get(node_span["status"], node_span["status"])
        tint = "32" if status == "ok" else "31" if status == "FAIL" else "33"
        node = report["nodes"].get(node_span["name"], {})
        dur = node_span.get("duration_ms")
        lines.append(
            f"  {paint(status.ljust(4), tint)} {node_span['name']}"
            f"  attempt {node_span['attributes'].get('attempt')}"
            + (f"  {dur:.1f}ms" if dur is not None else "")
            + (f"  [{node.get('label')}]" if node.get("label") else "")
        )
        for eff in node_span["children"]:
            attrs = eff["attributes"]
            extra = []
            if attrs.get("usage"):
                extra.append(f"{attrs['usage'].get('input_tokens', 0)}+{attrs['usage'].get('output_tokens', 0)} tok")
            if attrs.get("replayed"):
                extra.append("replayed")
            if attrs.get("injection_signals"):
                extra.append(paint("injection signals", "35"))
            mark = "x" if eff["status"] == "error" else "-"
            lines.append(f"       {mark} {eff['name']}  {eff.get('duration_ms') or 0:.1f}ms  {' '.join(extra)}")
        if node_span["status"] == "error" and node_span["attributes"].get("error"):
            err = node_span["attributes"]["error"]
            lines.append(f"       ! {err.get('code')}: {err.get('message')}")
    for title, key in (("policy", "policy_decisions"), ("approvals", "approvals"), ("retries", "retries"),
                       ("verification", "verifications"), ("divergences", "divergences")):
        items = report.get(key) or []
        if items:
            lines.append("")
            lines.append(f"  {title}:")
            lines.extend(f"    {json.dumps(i, default=str)[:220]}" for i in items)
    if report.get("error"):
        lines.append("")
        lines.append(paint(f"  error: {report['error'].get('code')}: {report['error'].get('message')}", "31"))
    lines.append("")
    lines.append(f"  output ({report.get('output_label')}):")
    out = report.get("output")
    text = out if isinstance(out, str) else json.dumps(out, indent=2, default=str)
    lines.extend("    " + line for line in str(text).splitlines()[:40])
    return "\n".join(lines)


def render_html(report: dict[str, Any]) -> str:
    """Self-contained HTML timeline (no external assets), safe to attach to bug reports."""
    trace = report["trace"]
    t0 = trace["start"]
    t1 = trace["end"] or max(
        [c["end"] or c["start"] for c in trace["children"]] + [t0 + 0.001]
    )
    span = max(t1 - t0, 1e-6)
    rows = []
    for node in trace["children"]:
        left = (node["start"] - t0) / span * 100
        width = max(((node["end"] or t1) - node["start"]) / span * 100, 0.4)
        info = report["nodes"].get(node["name"], {})
        rows.append(
            f'<tr><td class="n">{html.escape(node["name"])}</td><td>{html.escape(node["status"])}</td>'
            f'<td>{html.escape(str(info.get("label") or ""))}</td>'
            f'<td class="bar"><div class="b {html.escape(node["status"])}" style="left:{left:.2f}%;width:{width:.2f}%"'
            f' title="{node.get("duration_ms")} ms"></div></td></tr>'
        )
        for eff in node["children"]:
            left = (eff["start"] - t0) / span * 100
            width = max(((eff["end"] or eff["start"]) - eff["start"]) / span * 100, 0.3)
            rows.append(
                f'<tr class="e"><td class="n">&nbsp;&nbsp;{html.escape(eff["name"])}</td><td>{html.escape(eff["status"])}</td><td></td>'
                f'<td class="bar"><div class="b eff" style="left:{left:.2f}%;width:{width:.2f}%"></div></td></tr>'
            )
    data = html.escape(json.dumps({k: v for k, v in report.items() if k != "trace"}, indent=2, default=str))
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Cairn run {html.escape(report['run_id'])}</title>
<style>
:root{{--bg:#fff;--fg:#1d2330;--muted:#6b7280;--ok:#2f9e6b;--err:#d64545;--wait:#d99a1e;--eff:#5b7bd5;--line:#e5e7eb}}
@media (prefers-color-scheme: dark){{:root{{--bg:#12151c;--fg:#e6e8ee;--muted:#9aa3b2;--line:#2a2f3a}}}}
body{{background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,sans-serif;margin:24px;max-width:1200px}}
table{{width:100%;border-collapse:collapse}} td{{padding:3px 6px;border-bottom:1px solid var(--line)}}
td.n{{font-family:ui-monospace,monospace;white-space:nowrap}} tr.e td{{color:var(--muted);font-size:12px}}
td.bar{{position:relative;width:55%}} .b{{position:absolute;top:5px;height:12px;border-radius:3px;background:var(--ok)}}
.b.error{{background:var(--err)}} .b.waiting{{background:var(--wait)}} .b.skipped{{background:var(--muted)}} .b.eff{{background:var(--eff);height:8px;top:7px}}
pre{{background:rgba(127,127,127,.08);padding:12px;overflow:auto;border-radius:6px}}
</style></head><body>
<h1>Run {html.escape(report['run_id'])} <small>({html.escape(report['status'])})</small></h1>
<p>{html.escape(str(report.get('goal') or ''))}</p>
<p>Output provenance: <b>{html.escape(str(report.get('output_label')))}</b></p>
<table>{''.join(rows)}</table>
<h2>Report</h2><pre>{data}</pre></body></html>"""
