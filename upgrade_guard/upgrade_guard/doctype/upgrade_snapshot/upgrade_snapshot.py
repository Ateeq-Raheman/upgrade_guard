from html import escape

import frappe
from frappe.model.document import Document
from frappe.utils import now_datetime

from upgrade_guard.engine import storage

FILENAME = "snapshot.json"


class UpgradeSnapshot(Document):
	pass


@frappe.whitelist()
def start_snapshot(name):
	frappe.only_for("System Manager")
	frappe.db.set_value("Upgrade Snapshot", name, {"status": "Running", "error": ""}, update_modified=False)
	frappe.db.commit()
	frappe.enqueue("upgrade_guard.upgrade_guard.doctype.upgrade_snapshot.upgrade_snapshot.run_snapshot_job", queue="long", timeout=3600, name=name, user=frappe.session.user)


def run_snapshot_job(name, user):
	from upgrade_guard.engine.snapshot import take

	doc = frappe.get_doc("Upgrade Snapshot", name)

	def progress(pct, msg):
		frappe.publish_realtime("upgrade_guard_progress", {"name": name, "pct": pct, "msg": msg}, user=user)

	try:
		specs = [s.strip() for s in (doc.report_specs or "").splitlines() if s.strip()]
		snap = take(specs, int(doc.max_print or 50), progress=progress)
		storage.save_json("Upgrade Snapshot", name, FILENAME, snap)
		bad_prints = [k for k, v in snap["prints"].items() if v.get("status") == "error"]
		bad_reports = [k for k, v in snap["reports"].items() if v.get("status") == "error"]
		rows = "".join(f"<tr><td>{escape(k)}</td><td>{escape(str(v))}</td></tr>" for k, v in snap["totals"].items())
		html = (f"<p><b>{len(snap['doctypes'])}</b> DocTypes counted · <b>{len(snap['prints'])}</b> custom print formats rendered "
			f"({len(bad_prints)} failing) · <b>{len(snap['reports'])}</b> reports run ({len(bad_reports)} failing)</p>"
			+ (f"<p style='color:#dc2626'>Failing already: {escape(', '.join(bad_prints + bad_reports))}</p>" if bad_prints or bad_reports else "")
			+ f"<table class='table table-bordered'><tr><th>Total</th><th>Value</th></tr>{rows}</table>")
		doc.db_set({"status": "Completed", "taken_at": now_datetime(), "frappe_version": snap["frappe"], "summary_data": html})
		frappe.db.commit()
		progress(100, "Done")
	except Exception:
		frappe.db.rollback()
		frappe.db.set_value("Upgrade Snapshot", name, {"status": "Failed", "error": frappe.get_traceback()[-500:]})
		frappe.log_error("Upgrade Guard snapshot failed")
		frappe.db.commit()
		progress(100, "Failed — see Error Log")


@frappe.whitelist()
def compare_snapshots(before, after):
	"""Compare two snapshots. `before` is the older one."""
	frappe.only_for("System Manager")
	from upgrade_guard.engine.snapshot import compare

	a = storage.load_json("Upgrade Snapshot", before, FILENAME)
	b = storage.load_json("Upgrade Snapshot", after, FILENAME)
	res = compare(a, b)
	res["header"] = f"{before}: frappe {a['frappe']} ({a['taken_at']})  →  {after}: frappe {b['frappe']} ({b['taken_at']})"
	return res
