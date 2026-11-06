import json

import frappe
from frappe.model.document import Document
from frappe.utils import now_datetime

MAX_ROWS = 3000


class UpgradeReadinessCheck(Document):
	def validate(self):
		if int(self.target_version) <= int(self.source_version):
			frappe.throw("Target version must be higher than the current version")


@frappe.whitelist()
def start_scan(name):
	frappe.only_for("System Manager")
	doc = frappe.get_doc("Upgrade Readiness Check", name)
	doc.db_set({"status": "Running", "verdict": ""}, update_modified=False)
	frappe.db.commit()
	frappe.enqueue("upgrade_guard.upgrade_guard.doctype.upgrade_readiness_check.upgrade_readiness_check.run_scan_job", queue="long", timeout=3600, name=name, user=frappe.session.user)


def run_scan_job(name, user):
	from upgrade_guard.engine.report import render_html
	from upgrade_guard.engine.scan import Scanner

	doc = frappe.get_doc("Upgrade Readiness Check", name)

	def progress(pct, msg):
		frappe.publish_realtime("upgrade_guard_progress", {"name": name, "pct": pct, "msg": msg}, user=user)

	try:
		apps = [a.strip() for a in (doc.apps_to_scan or "").split(",") if a.strip()] or None
		res = Scanner(int(doc.source_version), int(doc.target_version), apps=apps, include_official=bool(doc.include_official), include_db=bool(doc.include_db), progress=progress).run()
		doc.reload()
		doc.set("findings", [])
		for f in res["findings"][:MAX_ROWS]:
			doc.append("findings", {k: f[k] for k in ("rule_id", "title", "severity", "confidence", "category", "hop", "app", "loc_type", "reference", "line", "snippet", "message", "fix", "link", "autofixable", "manual")})
		sc = res["severity_counts"]
		doc.update(
			{
				"status": "Completed", "verdict": res["verdict"], "score": res["score"], "estimated_hours": res["estimated_hours"],
				"blockers": sc["blocker"], "high": sc["high"], "medium": sc["medium"], "low": sc["low"], "info": sc["info"],
				"summary_html": render_html(res), "warnings": "\n".join(res["warnings"]) + (f"\nOnly the first {MAX_ROWS} findings are listed; download the JSON for the full list." if len(res["findings"]) > MAX_ROWS else ""),
				"scanned_at": now_datetime(),
			}
		)
		doc.save(ignore_permissions=True)
		f = frappe.get_doc({"doctype": "File", "file_name": f"{name}.json", "attached_to_doctype": doc.doctype, "attached_to_name": name, "content": json.dumps(res, default=str), "is_private": 1})
		f.save(ignore_permissions=True)
		progress(100, "Done")
	except Exception:
		frappe.db.rollback()
		frappe.db.set_value("Upgrade Readiness Check", name, "status", "Failed")
		frappe.log_error("Upgrade Guard scan failed")
		frappe.db.commit()
		progress(100, "Failed — see Error Log")


def _plan(name, include_unsafe=False):
	from upgrade_guard.engine.fixer import build_plan

	doc = frappe.get_doc("Upgrade Readiness Check", name)
	apps = [a.strip() for a in (doc.apps_to_scan or "").split(",") if a.strip()] or None
	return build_plan(int(doc.source_version), int(doc.target_version), apps=apps, include_unsafe=bool(int(include_unsafe)), include_db=bool(doc.include_db))


@frappe.whitelist()
def preview_fixes(name, include_unsafe=0):
	"""Dry run: returns the unified diff of every automatic fix. Changes nothing."""
	frappe.only_for("System Manager")
	plan = _plan(name, include_unsafe)
	return {"diff": plan.diff(), "summary": plan.summary(), "skipped": sorted(set(plan.skipped))[:50]}


@frappe.whitelist()
def apply_fixes(name, include_unsafe=0):
	"""Applies the fixes with a backup. Only in developer mode, never by accident on production."""
	frappe.only_for("System Manager")
	if not frappe.conf.developer_mode:
		frappe.throw("Applying fixes edits app source files and is only allowed when developer_mode is on.")
	from upgrade_guard.engine.fixer import apply_plan

	plan = _plan(name, include_unsafe)
	bdir = apply_plan(plan)
	return {"backup": bdir, "summary": plan.summary()}
