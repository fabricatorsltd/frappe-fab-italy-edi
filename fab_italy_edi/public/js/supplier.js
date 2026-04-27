frappe.ui.form.on("Supplier", {
	refresh(frm) {
		if (!frm.is_new()) {
			return;
		}

		const supplierPreview = parse_json_field(frm.doc.fab_edi_supplier_preview_json);
		const payments = parse_json_field(frm.doc.fab_edi_payments_preview_json);
		if (!supplierPreview) {
			return;
		}

		const messages = [__("This Supplier was started from an inbound EDI invoice.")];
		if (!frm.doc.supplier_primary_address && has_supplier_address_preview(supplierPreview)) {
			messages.push(__("The primary supplier address will be created when you save."));
		}
		if (!frm.doc.supplier_primary_contact && has_supplier_contact_preview(supplierPreview)) {
			messages.push(__("The primary supplier contact will be created when you save."));
		}
		const payment = (Array.isArray(payments) ? payments : []).find((row) => row?.iban);
		if (!frm.doc.default_bank_account && payment?.iban) {
			messages.push(
				__("A default supplier bank account will be created from IBAN {0} when you save.", [
					payment.iban,
				]),
			);
		}

		frm.set_intro(messages.join(" "), "blue");
	},
});

function parse_json_field(value) {
	if (!value) {
		return null;
	}

	if (typeof value !== "string") {
		return value;
	}

	try {
		return JSON.parse(value);
	} catch (error) {
		return null;
	}
}

function has_supplier_address_preview(supplierPreview) {
	return Boolean(supplierPreview?.address_line1 && supplierPreview?.city && supplierPreview?.country);
}

function has_supplier_contact_preview(supplierPreview) {
	return Boolean(supplierPreview?.email || supplierPreview?.recipient_pec || supplierPreview?.phone);
}
