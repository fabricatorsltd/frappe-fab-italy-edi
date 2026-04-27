const AUTOFATTURA_WORKSPACE_STORAGE_KEY = "fab_italy_edi.autofattura_workspace";
const FAB_EDI_APP_NAME = "fab_italy_edi";

frappe.ui.form.on("Autofattura", {
	refresh(frm) {
		restore_autofattura_workspace();
		render_status_headline(frm);
		frm.set_intro(
			__("Business autofattura for foreign purchases. Purchase Invoice accounting stays on the source document."),
			"blue"
		);

		if (frm.doc.source_purchase_invoice) {
			frm.add_custom_button(__("Open Purchase Invoice"), () => {
				frappe.set_route("Form", "Purchase Invoice", frm.doc.source_purchase_invoice);
			}, __("FAB EDI"));
		}

		if (frm.doc.linked_edi_document) {
			frm.add_custom_button(__("Open EDI Transport Record"), () => {
				frappe.set_route("Form", "EDI Document", frm.doc.linked_edi_document);
			}, __("FAB EDI"));
		}

		if (frm.doc.validation_state !== "valid") {
			frm.add_custom_button(__("Confirm Review"), async () => {
				await frappe.call({
					method: "fab_italy_edi.autofattura.confirm_autofattura_review",
					args: { docname: frm.doc.name },
					freeze: true,
					freeze_message: __("Confirming autofattura review..."),
				});
				await frm.reload_doc();
			}, __("FAB EDI"));
		}

		const activeStates = new Set(["queued", "sending", "sent", "delivered", "accepted"]);
		if (!activeStates.has(frm.doc.transmission_state)) {
			frm.add_custom_button(
				frm.doc.transmission_state === "failed" || frm.doc.transmission_state === "rejected"
					? __("Retry SDI Send")
					: __("Send to SDI"),
				async () => {
					await frappe.call({
						method: "fab_italy_edi.autofattura.send_autofattura_to_sdi",
						args: { docname: frm.doc.name },
						freeze: true,
						freeze_message: __("Queueing autofattura for SDI..."),
					});
					await frm.reload_doc();
				},
				__("FAB EDI")
			);
		}
	},
});

function restore_autofattura_workspace() {
	const workspace =
		fab_italy_edi?.get_autofattura_sidebar?.() ||
		window.sessionStorage?.getItem(AUTOFATTURA_WORKSPACE_STORAGE_KEY);
	if (!workspace || !frappe.app?.sidebar || !frappe.boot?.workspace_sidebar_item) {
		return;
	}

	const saved_sidebar = frappe.boot.workspace_sidebar_item[workspace.toLowerCase()];
	if (!saved_sidebar) {
		return;
	}

	const current_workspace = frappe.app.sidebar.sidebar_title;
	if (!current_workspace || current_workspace === workspace) {
		return;
	}

	const current_sidebar = frappe.boot.workspace_sidebar_item[current_workspace.toLowerCase()];
	if (current_sidebar?.app !== FAB_EDI_APP_NAME) {
		return;
	}

	setTimeout(() => frappe.app.sidebar.setup(workspace), 0);
}

frappe.ui.form.on("EDI Autofattura Line", {
	quantity(frm, cdt, cdn) {
		update_autofattura_line_total(frm, cdt, cdn);
	},
	unit_price(frm, cdt, cdn) {
		update_autofattura_line_total(frm, cdt, cdn);
	},
	total_price(frm) {
		refresh_autofattura_totals(frm);
	},
	tax_rate(frm) {
		refresh_autofattura_totals(frm);
	},
	lines_add(frm) {
		refresh_autofattura_totals(frm);
	},
	lines_remove(frm) {
		refresh_autofattura_totals(frm);
	},
});

function update_autofattura_line_total(frm, cdt, cdn) {
	const row = locals[cdt][cdn];
	const quantity = flt(row.quantity || 0);
	const unitPrice = flt(row.unit_price || 0);
	const total = flt(quantity * unitPrice, 2);
	if (flt(row.total_price || 0, 2) !== total) {
		frappe.model.set_value(cdt, cdn, "total_price", total);
		return;
	}
	refresh_autofattura_totals(frm);
}

function refresh_autofattura_totals(frm) {
	const lines = frm.doc.lines || [];
	const netTotal = lines.reduce((sum, row) => sum + flt(row.total_price || 0), 0);
	const taxTotal = lines.reduce((sum, row) => {
		const taxRate = row.tax_rate == null || row.tax_rate === "" ? null : flt(row.tax_rate || 0);
		if (taxRate == null || taxRate <= 0) {
			return sum;
		}
		return sum + flt((flt(row.total_price || 0) * taxRate) / 100, 2);
	}, 0);
	frm.set_value("net_total", flt(netTotal, 2));
	frm.set_value("tax_total", flt(taxTotal, 2));
	frm.set_value("grand_total", flt(netTotal + taxTotal, 2));
}

function render_status_headline(frm) {
	frm.dashboard.clear_headline();

	[
		{
			label: __("Review: {0}", [formatAutofatturaState(frm.doc.validation_state || "draft")]),
			color: getValidationStateColor(frm.doc.validation_state),
		},
		{
			label: __("SDI: {0}", [formatAutofatturaState(frm.doc.transmission_state || "draft")]),
			color: getTransmissionStateColor(frm.doc.transmission_state),
		},
		frm.doc.latest_receipt_state
			? {
					label: __("Receipt: {0}", [formatAutofatturaState(frm.doc.latest_receipt_state)]),
					color: getReceiptStateColor(frm.doc.latest_receipt_state),
				}
			: null,
	]
		.filter(Boolean)
		.forEach((indicator) => frm.dashboard.add_indicator(indicator.label, indicator.color));
}

function formatAutofatturaState(state) {
	const labels = {
		draft: __("Draft"),
		not_validated: __("Needs review"),
		valid: __("Reviewed"),
		invalid: __("Invalid"),
		validation_failed: __("Validation failed"),
		ready: __("Ready"),
		queued: __("Queued"),
		sending: __("Sending"),
		sent: __("Sent"),
		delivered: __("Delivered"),
		accepted: __("Accepted"),
		rejected: __("Rejected"),
		failed: __("Failed"),
		cancelled: __("Cancelled"),
		unknown_pending: __("Pending"),
	};

	return labels[state] || frappe.utils.to_title_case((state || "").replaceAll("_", " "));
}

function getValidationStateColor(state) {
	switch (state) {
		case "valid":
			return "green";
		case "invalid":
		case "validation_failed":
			return "red";
		case "not_validated":
		case "draft":
		default:
			return "orange";
	}
}

function getTransmissionStateColor(state) {
	switch (state) {
		case "accepted":
		case "delivered":
			return "green";
		case "ready":
		case "queued":
		case "sent":
			return "blue";
		case "sending":
			return "orange";
		case "rejected":
		case "failed":
			return "red";
		case "cancelled":
			return "gray";
		case "draft":
		default:
			return "orange";
	}
}

function getReceiptStateColor(state) {
	switch (state) {
		case "accepted":
		case "delivered":
			return "green";
		case "queued":
		case "sent":
		case "unknown_pending":
			return "blue";
		case "rejected":
		case "failed":
		case "cancelled":
			return "red";
		default:
			return "orange";
	}
}
