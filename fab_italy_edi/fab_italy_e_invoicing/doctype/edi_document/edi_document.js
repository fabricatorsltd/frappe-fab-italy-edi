frappe.ui.form.on("EDI Document", {
	refresh(frm) {
		if (frm.doc.document_kind !== "autofattura") {
			return;
		}

		frm.set_intro(
			__(
				"Edit the legal autofattura content here. Purchase Invoice tax rows are kept separate from the autofattura VAT declaration."
			),
			"blue"
		);
		frm.add_custom_button(__("Open Autofatture Dashboard"), () => frappe.set_route("autofatture"));
		refresh_autofattura_totals(frm);
	},
});

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
	autofattura_lines_add(frm) {
		refresh_autofattura_totals(frm);
	},
	autofattura_lines_remove(frm) {
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
	const lines = frm.doc.autofattura_lines || [];
	const netTotal = lines.reduce((sum, row) => sum + flt(row.total_price || 0), 0);
	const taxTotal = lines.reduce((sum, row) => {
		const taxRate = row.tax_rate == null || row.tax_rate === "" ? null : flt(row.tax_rate || 0);
		if (taxRate == null || taxRate <= 0) {
			return sum;
		}
		return sum + flt((flt(row.total_price || 0) * taxRate) / 100, 2);
	}, 0);
	frm.set_value("autofattura_net_total", flt(netTotal, 2));
	frm.set_value("autofattura_tax_total", flt(taxTotal, 2));
	frm.set_value("autofattura_grand_total", flt(netTotal + taxTotal, 2));
}
