"""Store/load JSON blobs as private File attachments (keeps big payloads out of the form).

Frappe renames duplicate attachment names (snapshot.json -> snapshot3f2a1c.json), so files are
matched by name prefix and the newest one wins."""
import json
import os

import frappe


def _files(doctype, name, filename):
	stem = os.path.splitext(filename)[0]
	return frappe.get_all(
		"File",
		filters={"attached_to_doctype": doctype, "attached_to_name": name, "file_name": ["like", f"{stem}%.json"]},
		fields=["name"],
		order_by="creation desc",
	)


def save_json(doctype, name, filename, data):
	for f in _files(doctype, name, filename):
		frappe.delete_doc("File", f.name, ignore_permissions=True, force=True)
	frappe.get_doc({"doctype": "File", "file_name": filename, "attached_to_doctype": doctype, "attached_to_name": name, "content": json.dumps(data, default=str), "is_private": 1}).save(ignore_permissions=True)


def load_json(doctype, name, filename):
	files = _files(doctype, name, filename)
	if not files:
		frappe.throw(f"No saved data found for {name}. Run it first.")
	content = frappe.get_doc("File", files[0].name).get_content()
	return json.loads(content if isinstance(content, str) else content.decode())
