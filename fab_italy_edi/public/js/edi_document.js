const AUTOFATTURA_WORKSPACE_STORAGE_KEY = "fab_italy_edi.autofattura_workspace";

frappe.ui.form.on("EDI Document", {
	refresh(frm) {
		if (frm.doc.document_kind === "autofattura") {
			setup_autofattura_document_actions(frm);
		}

		if (frm.doc.document_kind !== "supplier_invoice_import") {
			return;
		}

		frm.add_custom_button(__("Review Import Data"), async () => {
			const preview = await get_supplier_invoice_preview(frm);
			show_supplier_invoice_preview(preview);
		}, __("FAB EDI"));

		frm.add_custom_button(__("Create Supplier"), async () => {
			const preview = await get_supplier_invoice_preview(frm);
			frappe.route_options = preview.supplier_route_options || {};
			frappe.new_doc("Supplier");
		}, __("FAB EDI"));

		frm.add_custom_button(
			frm._fab_edi_purchase_invoice ? __("Open Purchase Invoice") : __("Create Purchase Invoice Draft"),
			async () => {
				const preview = await get_supplier_invoice_preview(frm, { force: true });
				if (preview.purchase_invoice) {
					frappe.set_route("Form", "Purchase Invoice", preview.purchase_invoice);
					return;
				}

				const fields = [
					{
						fieldname: "supplier",
						label: __("Supplier"),
						fieldtype: "Link",
						options: "Supplier",
						reqd: 1,
						default: preview.default_supplier,
					},
				];

				frappe.prompt(fields, async (values) => {
					const response = await frappe.call({
						method:
							"fab_italy_edi.purchase_invoice_import.create_purchase_invoice_draft_from_edi_document",
						args: {
							docname: frm.doc.name,
							supplier: values.supplier,
						},
					});
					const purchase_invoice = response.message.purchase_invoice;
					frm._fab_edi_purchase_invoice = purchase_invoice;
					frappe.set_route("Form", "Purchase Invoice", purchase_invoice);
				}, __("Create Purchase Invoice Draft"), __("Create"));
			},
			__("FAB EDI")
		);
	},
});

function setup_autofattura_document_actions(frm) {
	const typeLabel = get_autofattura_type_label(frm.doc.autofattura_document_type);
	const helpText = get_autofattura_type_help(frm.doc.autofattura_document_type);
	frm.set_intro(
		__(
			"This is the internal autofattura transport record. Use the Autofattura form for business editing. {0} {1}",
			[`<strong>${frappe.utils.escape_html(typeLabel)}</strong>`, frappe.utils.escape_html(helpText)]
		),
		"blue"
	);

	if (frm.doc.source_doctype === "Autofattura" && frm.doc.source_name) {
		frm.add_custom_button(__("Open Autofattura"), () => {
			remember_autofattura_workspace();
			frappe.set_route("Form", "Autofattura", frm.doc.source_name);
		}, __("FAB EDI"));
	}

	if (frm.doc.source_doctype === "Purchase Invoice" && frm.doc.source_name) {
		frm.add_custom_button(__("Open Purchase Invoice"), () => {
			frappe.set_route("Form", "Purchase Invoice", frm.doc.source_name);
		}, __("FAB EDI"));
	}

	frm.add_custom_button(__("Open Autofatture"), () => {
		frappe.set_route("List", "Autofattura");
	}, __("FAB EDI"));
}

function remember_autofattura_workspace() {
	if (fab_italy_edi?.remember_autofattura_sidebar) {
		fab_italy_edi.remember_autofattura_sidebar();
		return;
	}

	const workspace = frappe.app?.sidebar?.sidebar_title;
	if (!workspace) {
		return;
	}

	window.sessionStorage?.setItem(AUTOFATTURA_WORKSPACE_STORAGE_KEY, workspace);
}

async function get_supplier_invoice_preview(frm, options = {}) {
	if (!options.force && frm._fab_edi_preview) {
		return frm._fab_edi_preview;
	}

	const response = await frappe.call({
		method: "fab_italy_edi.purchase_invoice_import.get_incoming_supplier_invoice_preview",
		args: { docname: frm.doc.name },
	});
	frm._fab_edi_preview = response.message;
	frm._fab_edi_purchase_invoice = response.message.purchase_invoice;
	return response.message;
}

function show_supplier_invoice_preview(preview) {
	const dialog = new frappe.ui.Dialog({
		title: __("Inbound Supplier Invoice"),
		size: "extra-large",
		fields: [
			{
				fieldtype: "HTML",
				fieldname: "summary_html",
				options: build_preview_html(preview),
			},
		],
	});
	dialog.show();
}

function build_preview_html(preview) {
	const supplier = preview.supplier || {};
	const invoice = preview.invoice || {};
	const existingSuppliers = (preview.existing_suppliers || [])
		.map(
			(row) =>
				`<li><strong>${escape_html(row.supplier_name || row.name)}</strong>${row.tax_id ? ` - ${escape_html(row.tax_id)}` : ""}</li>`
		)
		.join("");
	const items = (preview.items || [])
		.map(
			(item) => `
				<tr>
					<td>${escape_html(item.line_no || "")}</td>
					<td>${escape_html(item.description || item.item_name || "")}</td>
					<td class="text-right">${format_number(item.qty)}</td>
					<td>${escape_html(item.uom || "")}</td>
					<td class="text-right">${format_currency_value(item.rate, invoice.currency)}</td>
					<td class="text-right">${format_currency_value(item.amount, invoice.currency)}</td>
				</tr>`
		)
		.join("");
	const taxes = (preview.taxes || [])
		.map(
			(tax) => `
				<tr>
					<td>${escape_html(tax.description || "")}</td>
					<td class="text-right">${format_number(tax.tax_rate)}</td>
					<td class="text-right">${format_currency_value(tax.taxable_amount, invoice.currency)}</td>
					<td class="text-right">${format_currency_value(tax.tax_amount, invoice.currency)}</td>
				</tr>`
		)
		.join("");
	const payments = (preview.payments || [])
		.map(
			(payment) =>
				`<li>${escape_html(payment.mode || __("Unknown mode"))}${
					payment.due_date ? ` - ${escape_html(payment.due_date)}` : ""
				}${payment.iban ? ` - ${escape_html(payment.iban)}` : ""}${
					payment.payment_amount ? ` - ${format_currency_value(payment.payment_amount, invoice.currency)}` : ""
				}</li>`
		)
		.join("");
	const attachments = (preview.attachments || [])
		.map(
			(attachment) =>
				`<li>${escape_html(attachment.name || __("Attachment"))}${
					attachment.format ? ` (${escape_html(attachment.format)})` : ""
				}${attachment.description ? ` - ${escape_html(attachment.description)}` : ""}</li>`
		)
		.join("");
	const notes = (invoice.notes || []).map((note) => `<li>${escape_html(note)}</li>`).join("");

	return `
		<div style="max-height:70vh; overflow:auto;">
			<div class="row">
				<div class="col-sm-6">
					<h5>${__("Supplier")}</h5>
					<p>
						<strong>${escape_html(supplier.display_name || __("Unknown Supplier"))}</strong><br>
						${supplier.tax_id ? `${__("Tax ID")}: ${escape_html(supplier.tax_id)}<br>` : ""}
						${supplier.email ? `${__("Email")}: ${escape_html(supplier.email)}<br>` : ""}
						${supplier.address_line1 ? `${escape_html(supplier.address_line1)}<br>` : ""}
						${[supplier.pincode, supplier.city, supplier.province, supplier.country].filter(Boolean).map(escape_html).join(" ")}
					</p>
				</div>
				<div class="col-sm-6">
					<h5>${__("Invoice")}</h5>
					<p>
						${invoice.bill_no ? `${__("Number")}: <strong>${escape_html(invoice.bill_no)}</strong><br>` : ""}
						${invoice.bill_date ? `${__("Date")}: ${escape_html(invoice.bill_date)}<br>` : ""}
						${invoice.due_date ? `${__("Due Date")}: ${escape_html(invoice.due_date)}<br>` : ""}
						${invoice.document_type ? `${__("Document Type")}: ${escape_html(invoice.document_type)}<br>` : ""}
						${preview.canonical_identifier ? `${__("File")}: ${escape_html(preview.canonical_identifier)}<br>` : ""}
						${__("Net Total")}: ${format_currency_value(invoice.total_net_amount, invoice.currency)}<br>
						${__("Tax Total")}: ${format_currency_value(invoice.total_tax_amount, invoice.currency)}<br>
						${__("Grand Total")}: <strong>${format_currency_value(invoice.total_amount, invoice.currency)}</strong>
					</p>
				</div>
			</div>
			${existingSuppliers ? `<h5>${__("Matching Suppliers")}</h5><ul>${existingSuppliers}</ul>` : `<p class="text-warning">${__("No matching Supplier found yet. Use Create Supplier, then return here to import the draft.")}</p>`}
			${notes ? `<h5>${__("Notes")}</h5><ul>${notes}</ul>` : ""}
			<h5>${__("Items")}</h5>
			<table class="table table-bordered">
				<thead>
					<tr>
						<th>${__("Line")}</th>
						<th>${__("Description")}</th>
						<th class="text-right">${__("Qty")}</th>
						<th>${__("UOM")}</th>
						<th class="text-right">${__("Rate")}</th>
						<th class="text-right">${__("Amount")}</th>
					</tr>
				</thead>
				<tbody>${items || `<tr><td colspan="6">${__("No importable lines found.")}</td></tr>`}</tbody>
			</table>
			<h5>${__("Tax Summary")}</h5>
			<table class="table table-bordered">
				<thead>
					<tr>
						<th>${__("Description")}</th>
						<th class="text-right">${__("Rate %")}</th>
						<th class="text-right">${__("Taxable")}</th>
						<th class="text-right">${__("Tax")}</th>
					</tr>
				</thead>
				<tbody>${taxes || `<tr><td colspan="4">${__("No tax rows found.")}</td></tr>`}</tbody>
			</table>
			${payments ? `<h5>${__("Payments")}</h5><ul>${payments}</ul>` : ""}
			${attachments ? `<h5>${__("Attachments")}</h5><ul>${attachments}</ul>` : ""}
		</div>`;
}

function format_currency_value(value, currency) {
	return format_number(value) ? format_currency(value || 0, currency) : format_currency(0, currency);
}

function format_number(value) {
	const numericValue = Number(value || 0);
	return frappe.format(numericValue, { fieldtype: "Float" });
}

function escape_html(value) {
	return frappe.utils.escape_html(value == null ? "" : String(value));
}

function get_autofattura_type_label(documentType) {
	return {
		TD17: __("TD17 - Purchase of services from abroad"),
		TD18: __("TD18 - Purchase of goods from EU suppliers"),
		TD19: __("TD19 - Purchase of goods from non-EU suppliers"),
	}[documentType] || documentType || __("Autofattura");
}

function get_autofattura_type_help(documentType) {
	return {
		TD17: __("Italian taxonomy code for autofattura on services purchased from abroad."),
		TD18: __("Italian taxonomy code for autofattura on goods purchased from EU suppliers."),
		TD19: __("Italian taxonomy code for autofattura on goods purchased from non-EU suppliers."),
	}[documentType] || "";
}
