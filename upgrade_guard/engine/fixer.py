"""Auto-fixer.  Dry-run by default.  Every applied change is backed up and can be undone."""
import ast
import difflib
import json
import os
import re
import shutil
import subprocess
from datetime import datetime

import frappe

from upgrade_guard.engine.scan import Scanner

REF_RE = re.compile(r"^(.+?): (.+) \[(.+)\]$")


class FixPlan:
	def __init__(self):
		self.files = {}    # abs path -> dict(app, rel, old, new, rules)
		self.records = []  # dict(doctype, name, field, old, new, rules)
		self.skipped = []  # (reason, reference, rule)

	def diff(self):
		out = []
		for path, f in self.files.items():
			out += difflib.unified_diff(f["old"].splitlines(True), f["new"].splitlines(True), f"a/{f['app']}/{f['rel']}", f"b/{f['app']}/{f['rel']}")
		for r in self.records:
			out += difflib.unified_diff(r["old"].splitlines(True), r["new"].splitlines(True), f"a/{r['doctype']}/{r['name']}/{r['field']}", f"b/{r['doctype']}/{r['name']}/{r['field']}")
		return "".join(out)

	def summary(self):
		return {"files": len(self.files), "records": len(self.records), "skipped": len(self.skipped), "rules": sorted({x for f in self.files.values() for x in f["rules"]} | {x for r in self.records for x in r["rules"]})}


def _apply(text, line_no, autofix, rule_id, state):
	"""Apply an autofix definition to one line (line_no>0) or the whole text (0)."""
	def run(chunk):
		changed = chunk
		for rep in autofix.get("replace", []):
			if rep.get("unless") and re.search(rep["unless"], changed):
				continue
			changed = re.sub(rep["pattern"], rep["with"], changed, flags=re.M)
		for k, v in (autofix.get("json_set") or {}).items():
			changed = re.sub(rf'("{re.escape(k)}"\s*:\s*)"[^"]*"', rf'\1"{v}"', changed)
		return changed

	if line_no and line_no > 0:
		lines = text.splitlines(True)
		if line_no <= len(lines):
			new = run(lines[line_no - 1])
			if new != lines[line_no - 1]:
				lines[line_no - 1] = new
				state.add(rule_id)
			return "".join(lines)
		return text
	new = run(text)
	if new != text:
		state.add(rule_id)
	return new


def _valid(path_or_field, old, new):
	if path_or_field.endswith(".py") or path_or_field in ("script", "report_script"):
		try:
			ast.parse(old)
		except SyntaxError:
			return True  # was already broken; do not block
		try:
			ast.parse(new)
		except SyntaxError:
			return False
	elif path_or_field.endswith(".json"):
		try:
			json.loads(new)
		except ValueError:
			return False
	return True


def build_plan(source, target, apps=None, rule_ids=None, include_unsafe=False, include_db=True):
	sc = Scanner(source, target, apps=apps, include_db=include_db, max_findings=10**9)
	sc.run()
	rules = {r["id"]: r for r in sc.rules}
	plan = FixPlan()
	groups = {}
	for f in sc.findings:
		rule = rules.get(f["rule_id"])
		if not (rule and rule.get("autofix")) or f["loc_type"] not in ("File", "Record"):
			continue
		if rule_ids and f["rule_id"] not in rule_ids:
			continue
		if rule["autofix"].get("safe") is False and not include_unsafe:
			plan.skipped.append(("unsafe rule (use --include-unsafe)", f["reference"], f["rule_id"]))
			continue
		groups.setdefault((f["loc_type"], f["app"], f["reference"]), []).append(f)

	for (loc_type, app, ref), items in groups.items():
		if loc_type == "File":
			path = os.path.join(os.path.dirname(sc.app_dirs[app]), ref)
			try:
				with open(path, encoding="utf-8") as fh:
					old = fh.read()
			except OSError:
				continue
			text, used = old, set()
			for f in sorted(items, key=lambda x: x["line"]):
				text = _apply(text, f["line"], rules[f["rule_id"]]["autofix"], f["rule_id"], used)
			if text == old:
				continue
			if not _valid(ref, old, text):
				plan.skipped.append(("result would not parse", ref, ",".join(sorted(used))))
				continue
			plan.files[path] = {"app": app, "rel": ref, "old": old, "new": text, "rules": sorted(used)}
		else:
			m = REF_RE.match(ref)
			if not m:
				continue
			dt, name, field = m.groups()
			old = frappe.db.get_value(dt, name, field) or ""
			text, used = old, set()
			for f in sorted(items, key=lambda x: x["line"]):
				text = _apply(text, f["line"], rules[f["rule_id"]]["autofix"], f["rule_id"], used)
			if text == old:
				continue
			if not _valid(field, old, text):
				plan.skipped.append(("result would not parse", ref, ",".join(sorted(used))))
				continue
			plan.records.append({"doctype": dt, "name": name, "field": field, "old": old, "new": text, "rules": sorted(used)})
	plan._app_roots = {a: os.path.dirname(p) for a, p in sc.app_dirs.items()}
	return plan


def _git_dirty(root):
	try:
		r = subprocess.run(["git", "-C", root, "status", "--porcelain"], capture_output=True, text=True, timeout=20)
		return r.returncode == 0 and bool(r.stdout.strip())
	except Exception:
		return False


def apply_plan(plan, force=False):
	apps = {f["app"] for f in plan.files.values()}
	dirty = [a for a in apps if _git_dirty(plan._app_roots[a])]
	if dirty and not force:
		raise frappe.ValidationError(f"Uncommitted git changes in: {', '.join(dirty)}. Commit or stash first (or force).")
	ts = datetime.now().strftime("%Y%m%d_%H%M%S")
	bdir = os.path.join(frappe.utils.get_bench_path(), "upgrade_guard_backups", ts)
	os.makedirs(bdir, exist_ok=True)
	manifest = {"created": ts, "site": frappe.local.site, "files": [], "records": []}
	for path, f in plan.files.items():
		dest = os.path.join(bdir, "files", f["app"], f["rel"])
		os.makedirs(os.path.dirname(dest), exist_ok=True)
		shutil.copy2(path, dest)
		with open(path, "w", encoding="utf-8") as fh:
			fh.write(f["new"])
		manifest["files"].append({"path": path, "backup": dest, "rules": f["rules"]})
	for r in plan.records:
		manifest["records"].append({k: r[k] for k in ("doctype", "name", "field", "old", "rules")})
		frappe.db.set_value(r["doctype"], r["name"], r["field"], r["new"])
	frappe.db.commit()
	with open(os.path.join(bdir, "manifest.json"), "w") as fh:
		json.dump(manifest, fh, indent=1)
	return bdir


def undo(backup_dir):
	with open(os.path.join(backup_dir, "manifest.json")) as fh:
		m = json.load(fh)
	for f in m["files"]:
		shutil.copy2(f["backup"], f["path"])
	for r in m["records"]:
		frappe.db.set_value(r["doctype"], r["name"], r["field"], r["old"])
	frappe.db.commit()
	return len(m["files"]), len(m["records"])
