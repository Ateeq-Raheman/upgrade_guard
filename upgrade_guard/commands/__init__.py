import json
import os

import click

import frappe
from frappe.commands import get_site, pass_context


@click.command("upgrade-check")
@click.option("--source", type=int, help="Current major version (auto-detected if omitted)")
@click.option("--target", type=int, required=True, help="Major version to upgrade to, e.g. 16")
@click.option("--apps", help="Comma separated apps to scan (default: all custom apps)")
@click.option("--include-official", is_flag=True, help="Also scan frappe/erpnext/hrms source")
@click.option("--no-db", is_flag=True, help="Skip DB customizations (Server/Client Scripts ...)")
@click.option("--format", "fmt", type=click.Choice(["text", "json", "md", "html"]), default="text")
@click.option("--out", help="Write the report to this file")
@click.option("--min-severity", default="low", type=click.Choice(["blocker", "high", "medium", "low", "info"]))
@pass_context
def upgrade_check(context, target, source=None, apps=None, include_official=False, no_db=False, fmt="text", out=None, min_severity="low"):
	"""Scan the site and its apps for breaking changes between two Frappe versions (read-only)."""
	from upgrade_guard.engine.constants import SEVERITY_ORDER
	from upgrade_guard.engine.report import render
	from upgrade_guard.engine.scan import run_scan

	site = get_site(context)
	frappe.init(site)
	frappe.connect()
	try:
		res = run_scan(
			source=source, target=target,
			apps=[a.strip() for a in apps.split(",")] if apps else None,
			include_official=include_official, include_db=not no_db,
			progress=lambda p, m: click.echo(f"[{p:>3}%] {m}", err=True),
		)
	finally:
		frappe.destroy()
	keep = SEVERITY_ORDER[: SEVERITY_ORDER.index(min_severity) + 1]
	res["findings"] = [f for f in res["findings"] if f["severity"] in keep]
	text = json.dumps(res, indent=1, default=str) if fmt == "json" else render(res, fmt)
	if out:
		with open(out, "w", encoding="utf-8") as f:
			f.write(text)
		click.echo(f"Report written to {os.path.abspath(out)}")
	else:
		click.echo(text)
	raise SystemExit({"GO": 0, "GO WITH FIXES": 1, "NO-GO": 2}[res["verdict"]])




@click.command("upgrade-fix")
@click.option("--source", type=int)
@click.option("--target", type=int, required=True)
@click.option("--apps")
@click.option("--rules", help="Comma separated rule ids to fix (default: all auto-fixable)")
@click.option("--apply", "do_apply", is_flag=True, help="Write changes. Without it this is a dry run that prints a diff.")
@click.option("--include-unsafe", is_flag=True, help="Also apply fixes marked unsafe (review the diff first!)")
@click.option("--no-db", is_flag=True)
@click.option("--force", is_flag=True, help="Apply even if an app has uncommitted git changes")
@click.option("--out", help="Write the diff to this file")
@pass_context
def upgrade_fix(context, target, source=None, apps=None, rules=None, do_apply=False, include_unsafe=False, no_db=False, force=False, out=None):
	"""Preview (default) or apply safe automatic fixes, with backup and undo."""
	from upgrade_guard.engine.fixer import apply_plan, build_plan
	from upgrade_guard.engine.scan import detect_source_version

	site = get_site(context)
	frappe.init(site)
	frappe.connect()
	try:
		plan = build_plan(
			source or detect_source_version(), target,
			apps=[a.strip() for a in apps.split(",")] if apps else None,
			rule_ids=set(rules.split(",")) if rules else None,
			include_unsafe=include_unsafe, include_db=not no_db,
		)
		diff = plan.diff()
		if out:
			with open(out, "w") as f:
				f.write(diff)
		else:
			click.echo(diff)
		s = plan.summary()
		click.echo(f"\n{s['files']} file(s) and {s['records']} record(s) would change; {s['skipped']} skipped. Rules: {', '.join(s['rules']) or '-'}", err=True)
		for reason, ref, rule in sorted(set(plan.skipped))[:20]:
			click.echo(f"  skipped {rule} {ref}: {reason}", err=True)
		if do_apply:
			bdir = apply_plan(plan, force=force)
			click.echo(f"APPLIED. Backup + manifest: {bdir}\nUndo with: bench --site {site} upgrade-fix-undo {bdir}", err=True)
		else:
			click.echo("Dry run only. Add --apply to write these changes.", err=True)
	finally:
		frappe.destroy()


@click.command("upgrade-fix-undo")
@click.argument("backup_dir")
@pass_context
def upgrade_fix_undo(context, backup_dir):
	"""Restore everything changed by a previous upgrade-fix --apply."""
	from upgrade_guard.engine.fixer import undo

	site = get_site(context)
	frappe.init(site)
	frappe.connect()
	try:
		f, r = undo(backup_dir)
		click.echo(f"Restored {f} file(s) and {r} record(s).")
	finally:
		frappe.destroy()



@click.command("upgrade-snapshot")
@click.option("--out", required=True, help="JSON file to write")
@click.option("--reports", multiple=True, help="Report to run, optionally 'Name::{json filters}'. Repeatable.")
@click.option("--max-print", default=50, type=int)
@pass_context
def upgrade_snapshot(context, out, reports=(), max_print=50):
	"""Record counts, schema, totals, custom print renders and reports (read-only) for before/after comparison."""
	from upgrade_guard.engine.snapshot import take

	site = get_site(context)
	frappe.init(site)
	frappe.connect()
	try:
		snap = take(list(reports), max_print, progress=lambda p, m: click.echo(f"[{p:>3}%] {m}", err=True))
	finally:
		frappe.destroy()
	with open(out, "w") as f:
		json.dump(snap, f, indent=1)
	click.echo(f"Snapshot written to {os.path.abspath(out)} ({len(snap['doctypes'])} doctypes, {len(snap['prints'])} prints, {len(snap['reports'])} reports)")


@click.command("upgrade-compare")
@click.argument("before")
@click.argument("after")
def upgrade_compare(before, after):
	"""Compare two snapshots; exit code 1 if regressions were found."""
	from upgrade_guard.engine.snapshot import compare

	with open(before) as f:
		a = json.load(f)
	with open(after) as f:
		b = json.load(f)
	res = compare(a, b)
	click.echo(f"before: frappe {a['frappe']} ({a['taken_at']})   after: frappe {b['frappe']} ({b['taken_at']})")
	click.echo(f"\nREGRESSIONS ({len(res['regressions'])})")
	for r in res["regressions"]:
		click.echo(f"  ✗ {r}")
	click.echo(f"\nOther changes ({len(res['changes'])})")
	for r in res["changes"][:100]:
		click.echo(f"  · {r}")
	raise SystemExit(1 if res["regressions"] else 0)


@click.command("upgrade-runbook")
@click.option("--source", type=int)
@click.option("--target", type=int, required=True)
@click.option("--out")
@pass_context
def upgrade_runbook(context, target, source=None, out=None):
	"""Generate a tailored step-by-step upgrade + rollback runbook."""
	from upgrade_guard.engine.runbook import build
	from upgrade_guard.engine.scan import detect_source_version

	site = get_site(context)
	frappe.init(site)
	frappe.connect()
	try:
		text = build(source or detect_source_version(), target)
	finally:
		frappe.destroy()
	if out:
		with open(out, "w") as f:
			f.write(text)
		click.echo(f"Runbook written to {os.path.abspath(out)}")
	else:
		click.echo(text)


commands = [upgrade_check, upgrade_fix, upgrade_fix_undo, upgrade_snapshot, upgrade_compare, upgrade_runbook]
