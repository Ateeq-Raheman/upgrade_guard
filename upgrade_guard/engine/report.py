"""Render a scan result as text / markdown / html."""
from html import escape

from upgrade_guard.engine.constants import SEVERITY_ORDER

COLORS = {"GO": "#16a34a", "GO WITH FIXES": "#d97706", "NO-GO": "#dc2626"}
SEV_COLORS = {"blocker": "#dc2626", "high": "#ea580c", "medium": "#d97706", "low": "#2563eb", "info": "#6b7280"}


def _group(findings):
	g = {}
	for f in findings:
		g.setdefault((f["rule_id"], f["title"], f["severity"], f["fix"], f["link"], f["hop"]), []).append(f)
	return sorted(g.items(), key=lambda kv: (SEVERITY_ORDER.index(kv[0][2]), kv[0][0]))


def render(res, fmt="text"):
	if fmt == "html":
		return render_html(res)
	md = fmt == "md"
	h = (lambda s: f"\n## {s}\n") if md else (lambda s: f"\n=== {s} ===")
	L = [f"# Upgrade readiness: v{res['source']} → v{res['target']}" if md else f"UPGRADE READINESS v{res['source']} -> v{res['target']}"]
	L.append(f"Verdict: **{res['verdict']}**" if md else f"Verdict: {res['verdict']}")
	L.append(f"Score: {res['score']}/100   Estimated effort: ~{res['estimated_hours']} h   Rules: {res['rules_loaded']}   Files: {res['stats']['files_scanned']}   Records: {res['stats']['records_scanned']}")
	L.append("Findings: " + ", ".join(f"{s}={n}" for s, n in res["severity_counts"].items()))
	for w in res.get("warnings", []):
		L.append(f"WARNING: {w}")
	env = res["stats"].get("environment", {})
	if env:
		L.append(f"Environment: python {env.get('python')}, node {env.get('node')}, db {env.get('database')}")
	L.append(h("By app"))
	for app, c in sorted(res["per_app"].items()):
		L.append(f"- {app}: " + ", ".join(f"{s}={n}" for s, n in c.items() if n))
	L.append(h("Findings by rule"))
	for (rid, title, sev, fix, link, hop), items in _group(res["findings"]):
		L.append(f"\n[{sev.upper()}] {rid} ({hop}) {title} — {len(items)} location(s)")
		if fix:
			L.append(f"  Fix: {fix}")
		if link:
			L.append(f"  See: {link}")
		for f in items[:15]:
			loc = f"{f['app']} :: {f['reference']}" + (f":{f['line']}" if f["line"] else "")
			L.append(f"    - {loc}   {f['snippet'][:100]}")
		if len(items) > 15:
			L.append(f"    ... and {len(items) - 15} more")
	return "\n".join(L)


def render_html(res):
	e = escape
	rows = []
	for (rid, title, sev, fix, link, hop), items in _group(res["findings"]):
		locs = "".join(
			f"<li><code>{e(f['app'])} :: {e(f['reference'])}{(':' + str(f['line'])) if f['line'] else ''}</code><br><small>{e(f['snippet'][:160])}</small></li>"
			for f in items[:25]
		)
		more = f"<li>… and {len(items) - 25} more</li>" if len(items) > 25 else ""
		rows.append(
			f"<details><summary><b style='color:{SEV_COLORS[sev]}'>{sev.upper()}</b> {e(rid)} · {e(title)} <span style='color:#888'>({len(items)})</span></summary>"
			f"<p><b>Fix:</b> {e(fix)}" + (f" · <a href='{e(link)}'>reference</a>" if link else "") + f"</p><ul>{locs}{more}</ul></details>"
		)
	counts = " · ".join(f"<span style='color:{SEV_COLORS[s]}'>{s}: <b>{n}</b></span>" for s, n in res["severity_counts"].items())
	app_rows = "".join(f"<tr><td>{e(a)}</td>" + "".join(f"<td>{c[s]}</td>" for s in SEVERITY_ORDER) + "</tr>" for a, c in sorted(res["per_app"].items()))
	return (
		f"<div style='font-family:system-ui;max-width:1000px'>"
		f"<h2>Upgrade readiness: v{res['source']} → v{res['target']}</h2>"
		f"<div style='display:inline-block;padding:8px 18px;border-radius:8px;background:{COLORS[res['verdict']]};color:#fff;font-size:20px;font-weight:700'>{res['verdict']}</div>"
		f"<p>Score <b>{res['score']}/100</b> · est. effort <b>~{res['estimated_hours']} h</b> · {counts}</p>"
		f"<table class='table table-bordered'><tr><th>App</th>" + "".join(f"<th>{s}</th>" for s in SEVERITY_ORDER) + f"</tr>{app_rows}</table>"
		+ "".join(rows) + "</div>"
	)
