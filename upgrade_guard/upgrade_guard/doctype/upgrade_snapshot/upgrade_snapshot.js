const M = "upgrade_guard.upgrade_guard.doctype.upgrade_snapshot.upgrade_snapshot.";
frappe.ui.form.on("Upgrade Snapshot", {
	refresh(frm) {
		frm.get_field("summary_html").$wrapper.html(frm.doc.summary_data || `<p class="text-muted">${__("No results yet.")}</p>`);
		if (frm.is_new()) return;
		frm.add_custom_button(frm.doc.status === "Completed" ? __("Re-take snapshot") : __("Take snapshot"), () => {
			frm.save().then(() => frappe.call({ method: M + "start_snapshot", args: { name: frm.doc.name } }).then(() => frm.reload_doc()));
		}).addClass("btn-primary");
		if (frm.doc.status === "Completed") {
			frm.add_custom_button(__("Compare…"), () => {
				const d = new frappe.ui.Dialog({
					title: __("Compare snapshots"),
					fields: [
						{ fieldname: "before", label: __("Before (older, e.g. on v15)"), fieldtype: "Link", options: "Upgrade Snapshot", reqd: 1, get_query: () => ({ filters: { status: "Completed" } }) },
						{ fieldname: "after", label: __("After (newer, e.g. on v16)"), fieldtype: "Link", options: "Upgrade Snapshot", reqd: 1, default: frm.doc.name, get_query: () => ({ filters: { status: "Completed" } }) },
					],
					primary_action_label: __("Compare"),
					primary_action(v) {
						frappe.call({ method: M + "compare_snapshots", args: v, freeze: true }).then((r) => {
							d.hide();
							const li = (arr, mark, color) => arr.map((x) => `<li style="color:${color}">${mark} ${frappe.utils.escape_html(x)}</li>`).join("");
							const res = r.message;
							const html = `<p class="text-muted">${frappe.utils.escape_html(res.header)}</p>
								<h5 style="color:${res.regressions.length ? "#dc2626" : "#16a34a"}">${res.regressions.length ? res.regressions.length + " regression(s)" : "No regressions found"}</h5>
								<ul>${li(res.regressions, "✗", "#dc2626")}</ul>
								<h6>Other changes (${res.changes.length})</h6><ul>${li(res.changes.slice(0, 150), "·", "#6b7280")}</ul>`;
							frappe.msgprint({ title: __("Snapshot comparison"), message: html, wide: true });
						});
					},
				});
				d.show();
			});
		}
		frappe.realtime.off("upgrade_guard_progress");
		frappe.realtime.on("upgrade_guard_progress", (d) => {
			if (d.name !== frm.doc.name) return;
			frappe.show_progress(__("Snapshot"), d.pct, 100, d.msg);
			if (d.pct >= 100) { frappe.hide_progress(); frm.reload_doc(); }
		});
	},
});
