frappe.ui.form.on("Upgrade Readiness Check", {
	refresh(frm) {
		const colors = { GO: "green", "GO WITH FIXES": "orange", "NO-GO": "red" };
		if (frm.doc.verdict) frm.dashboard.set_headline(`<span class="indicator-pill ${colors[frm.doc.verdict]}">${frm.doc.verdict}</span> readiness ${frm.doc.score}/100 · ~${frm.doc.estimated_hours} h of fixes`);
		if (!frm.is_new()) {
			frm.add_custom_button(frm.doc.status === "Completed" ? __("Re-run scan") : __("Run scan"), () => {
				frm.save().then(() => frappe.call({ method: "upgrade_guard.upgrade_guard.doctype.upgrade_readiness_check.upgrade_readiness_check.start_scan", args: { name: frm.doc.name } }).then(() => frm.reload_doc()));
			}).addClass("btn-primary");
		}
		if (!frm.is_new() && frm.doc.status === "Completed") {
			const m = "upgrade_guard.upgrade_guard.doctype.upgrade_readiness_check.upgrade_readiness_check.";
			frm.add_custom_button(__("Preview auto-fixes"), () => {
				frappe.call({ method: m + "preview_fixes", args: { name: frm.doc.name }, freeze: true, freeze_message: __("Building fix plan…") }).then((r) => {
					const { diff, summary, skipped } = r.message;
					const d = new frappe.ui.Dialog({
						title: __("Auto-fix preview — {0} file(s), {1} record(s)", [summary.files, summary.records]),
						size: "extra-large",
						fields: [{ fieldtype: "HTML", fieldname: "diff" }],
						primary_action_label: __("Apply (with backup)"),
						primary_action() {
							frappe.confirm(__("Edit these files and records now? A backup with undo is created."), () => {
								frappe.call({ method: m + "apply_fixes", args: { name: frm.doc.name }, freeze: true }).then((x) => {
									d.hide();
									frappe.msgprint(__("Applied. Backup: {0}<br>Undo: <code>bench --site {1} upgrade-fix-undo {0}</code>", [x.message.backup, frappe.boot.sitename]));
								});
							});
						},
					});
					d.fields_dict.diff.$wrapper.html(`<pre style="max-height:60vh;overflow:auto">${frappe.utils.escape_html(diff || "Nothing to fix automatically.")}</pre><p class="text-muted">${skipped.length} skipped (unsafe or unparseable).</p>`);
					d.show();
				});
			}, __("Fix"));
		}
		frappe.realtime.off("upgrade_guard_progress");
		frappe.realtime.on("upgrade_guard_progress", (d) => {
			if (d.name !== frm.doc.name) return;
			frappe.show_progress(__("Scanning"), d.pct, 100, d.msg);
			if (d.pct >= 100) { frappe.hide_progress(); frm.reload_doc(); }
		});
	},
});
