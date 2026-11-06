"""Before/after regression snapshots.  READ-ONLY: take one on v15, one on the upgraded staging site, compare."""
import hashlib
import json
from datetime import datetime

import frappe

TOTALS = [
	("GL Entry", "debit"), ("GL Entry", "credit"), ("Stock Ledger Entry", "stock_value_difference"),
	("Sales Invoice", "grand_total"), ("Purchase Invoice", "grand_total"), ("Payment Entry", "paid_amount"),
	("Journal Entry", "total_debit"), ("Salary Slip", "net_pay"),
]


def _versions():
	out = {}
	for a in frappe.get_installed_apps():
		try:
			out[a] = frappe.get_attr(a + ".__version__")
		except Exception:
			out[a] = ""
	return out


def _sha(obj):
	return hashlib.sha1(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:12]


def take(reports=None, max_print=50, progress=lambda p, m: None):
	snap = {"taken_at": datetime.now().isoformat(timespec="seconds"), "site": frappe.local.site, "frappe": frappe.__version__,
		"apps": _versions(),
		"doctypes": {}, "schema": {}, "totals": {}, "prints": {}, "reports": {}}
	dts = frappe.get_all("DocType", filters={"issingle": 0, "is_virtual": 0}, fields=["name", "istable"])
	for i, d in enumerate(dts):
		if i % 50 == 0:
			progress(int(50 * i / max(len(dts), 1)), "Counting records and schema")
		try:
			meta = frappe.get_meta(d.name)
			snap["schema"][d.name] = sorted(f"{f.fieldname}:{f.fieldtype}" for f in meta.fields)
			if meta.is_submittable:
				rows = frappe.db.sql(f"select docstatus, count(*) from `tab{d.name}` group by docstatus")
				snap["doctypes"][d.name] = {str(k): v for k, v in rows}
			else:
				snap["doctypes"][d.name] = {"all": frappe.db.count(d.name)}
		except Exception as e:
			snap["doctypes"][d.name] = {"error": str(e)[:120]}
	for dt, col in TOTALS:
		try:
			cond = "where docstatus=1" if frappe.get_meta(dt).is_submittable else ""
			snap["totals"][f"{dt}.{col}"] = float(frappe.db.sql(f"select coalesce(sum(`{col}`),0) from `tab{dt}` {cond}")[0][0])
		except Exception:
			pass
	progress(55, "Rendering custom print formats")
	pfs = frappe.get_all("Print Format", filters={"disabled": 0, "standard": "No"}, fields=["name", "doc_type"], limit_page_length=max_print)
	for pf in pfs:
		try:
			doc = frappe.get_all(pf.doc_type, fields=["name"], order_by="creation desc", limit_page_length=1)
			if not doc:
				snap["prints"][pf.name] = {"status": "no-sample-document"}
				continue
			html = frappe.get_print(pf.doc_type, doc[0].name, print_format=pf.name)
			snap["prints"][pf.name] = {"status": "ok", "doc": doc[0].name, "length": len(html)}
		except Exception as e:
			snap["prints"][pf.name] = {"status": "error", "error": f"{type(e).__name__}: {str(e)[:150]}"}
	progress(80, "Running reports")
	for spec in reports or []:
		name, _, filt = spec.partition("::")
		try:
			from frappe.desk.query_report import run

			res = run(name, filters=json.loads(filt) if filt else {}, ignore_prepared_report=True)
			rows = res.get("result", [])
			snap["reports"][spec] = {"status": "ok", "rows": len(rows), "columns": len(res.get("columns", [])), "hash": _sha(rows)}
		except Exception as e:
			snap["reports"][spec] = {"status": "error", "error": f"{type(e).__name__}: {str(e)[:150]}"}
	progress(100, "Done")
	return snap


def compare(a, b, tol=1e-6):
	"""Return {'regressions': [...], 'changes': [...]}.  a = before, b = after."""
	reg, chg = [], []
	for dt, ca in a["doctypes"].items():
		cb = b["doctypes"].get(dt)
		if cb is None:
			reg.append(f"DocType removed: {dt}")
		elif "error" in cb and "error" not in ca:
			reg.append(f"{dt}: table unreadable after upgrade ({cb['error']})")
		elif ca != cb and "error" not in ca:
			(reg if sum(cb.values()) < sum(ca.values()) else chg).append(f"{dt}: record counts {ca} -> {cb}")
	for dt in set(b["doctypes"]) - set(a["doctypes"]):
		chg.append(f"New DocType: {dt}")
	for dt, fa in a["schema"].items():
		fb = b["schema"].get(dt)
		if fb is not None and fa != fb:
			gone = sorted(set(fa) - set(fb))
			added = sorted(set(fb) - set(fa))
			if gone:
				reg.append(f"{dt}: fields removed/changed {gone[:8]}")
			if added:
				chg.append(f"{dt}: fields added {added[:8]}")
	for k, va in a["totals"].items():
		vb = b["totals"].get(k)
		if vb is None or abs(va - vb) > max(tol, abs(va) * 1e-9):
			reg.append(f"Total {k}: {va} -> {vb}")
	for k, pa in a["prints"].items():
		pb = b["prints"].get(k, {"status": "missing"})
		if pa.get("status") == "ok" and pb.get("status") != "ok":
			reg.append(f"Print Format '{k}' now fails: {pb.get('error', pb.get('status'))}")
		elif pa.get("status") == "ok" and pb.get("length", 0) < 0.7 * pa["length"]:
			reg.append(f"Print Format '{k}' output shrank {pa['length']} -> {pb['length']} chars")
	for k, ra in a["reports"].items():
		rb = b["reports"].get(k, {"status": "missing"})
		if ra.get("status") == "ok" and rb.get("status") != "ok":
			reg.append(f"Report '{k}' now fails: {rb.get('error', rb.get('status'))}")
		elif ra.get("status") == "ok" and (ra["rows"], ra["hash"]) != (rb["rows"], rb["hash"]):
			reg.append(f"Report '{k}' output differs: rows {ra['rows']} -> {rb['rows']}")
	return {"regressions": reg, "changes": chg}
