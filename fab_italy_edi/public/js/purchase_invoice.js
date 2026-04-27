const AUTOFATTURA_WORKSPACE_STORAGE_KEY = "fab_italy_edi.autofattura_workspace";

frappe.ui.form.on("Purchase Invoice", {
	refresh(frm) {
		if (!frm.is_new()) {
			void update_autofattura_actions(frm);
		}

		const hasInboundEdiContext = is_inbound_edi_purchase_invoice(frm);
		if (hasInboundEdiContext) {
			update_inbound_invoice_intro(frm);
		} else {
			clear_supplier_route_options(frm);
		}

		if (!frm.doc.supplier && (hasInboundEdiContext || frm.is_new())) {
			configure_supplier_new_doc_handler(frm);
		} else {
			reset_supplier_new_doc_handler(frm);
		}

		if (!hasInboundEdiContext) {
			return;
		}

		frm.add_custom_button(__("Open EDI Document"), () => {
			frappe.set_route("Form", "EDI Document", frm.doc.fab_edi_document);
		}, __("FAB EDI"));

		if (!frm.doc.supplier) {
			frm.add_custom_button(__("Create Supplier from EDI"), async () => {
				await create_supplier_from_edi(frm);
			}, __("FAB EDI"));
		}
	},
});

function is_inbound_edi_purchase_invoice(frm) {
	return Boolean(frm.doc.fab_edi_imported && frm.doc.fab_edi_document);
}

async function update_autofattura_actions(frm) {
	if (frm.is_new() || !frm.doc.name || !frm.doc.company || !frm.doc.supplier) {
		return;
	}

	const context = await get_autofattura_context(frm);
	if (context.autofattura) {
		frm.add_custom_button(__("Open Autofattura"), () => {
			open_autofattura_form(context.autofattura);
		}, __("FAB EDI"));
	}

	if (context.requires_naming_series) {
		frm.add_custom_button(__("Configure Autofattura Sequence"), () => {
			open_edi_configuration(frm, context);
		}, __("FAB EDI"));
	}

	if (!context.can_prepare) {
		return;
	}

	frm.add_custom_button(__("Prepare Autofattura"), () => {
		open_autofattura_dialog(frm, context);
	}, __("FAB EDI"));
}

async function update_inbound_invoice_intro(frm) {
	const preview = await get_inbound_invoice_preview(frm);
	apply_supplier_route_options(frm, preview);
	const supplierName = preview.supplier?.display_name || __("Unknown Supplier");
	const gross = format_currency(preview.invoice?.total_amount || 0, preview.invoice?.currency);
	const needsSupplier = !frm.doc.supplier;
	const hasImportedTaxRows = (frm.doc.taxes || []).length > 0;
	const hasEdiTax = Number(preview.invoice?.total_tax_amount || 0) !== 0;
	const missingTaxCompletion = hasEdiTax && !hasImportedTaxRows;
	const unresolvedTaxBuckets = preview.unresolved_tax_buckets || [];

	const messages = [
		__("Inbound EDI invoice from {0}. Supplier total: {1}.", [supplierName.bold(), gross.bold()]),
	];
	if (needsSupplier) {
		messages.push(__("Create or select the Supplier before posting this invoice."));
	}
	if (missingTaxCompletion) {
		if (unresolvedTaxBuckets.length) {
			messages.push(
				__("Configure inbound tax mappings for {0} before submitting so ERPNext matches the supplier total.", [
					unresolvedTaxBuckets.map((bucket) => bucket.label).join(", "),
				])
			);
		} else {
			messages.push(__("Review the imported tax rows before submitting so ERPNext matches the supplier total."));
		}
	}

	frm.set_intro(messages.join(" "), needsSupplier || missingTaxCompletion ? "orange" : "blue");
}

async function get_inbound_invoice_preview(frm) {
	if (frm._fab_edi_preview) {
		return frm._fab_edi_preview;
	}

	const response = await frappe.call({
		method: "fab_italy_edi.purchase_invoice_import.get_incoming_supplier_invoice_preview",
		args: { docname: frm.doc.fab_edi_document },
	});
	frm._fab_edi_preview = response.message;
	return response.message;
}

async function get_autofattura_context(frm) {
	if (frm._fab_autofattura_context) {
		return frm._fab_autofattura_context;
	}

	const response = await frappe.call({
		method: "fab_italy_edi.autofattura.get_purchase_invoice_autofattura_context",
		args: { docname: frm.doc.name },
	});
	frm._fab_autofattura_context = response.message || {};
	return frm._fab_autofattura_context;
}

function open_edi_configuration(frm, context) {
	if (context.config_exists) {
		frappe.set_route("Form", "EDI Configuration", frm.doc.company);
		return;
	}

	frappe.new_doc("EDI Configuration", { company: frm.doc.company });
}

function open_autofattura_dialog(frm, context) {
	const dialog = new frappe.ui.Dialog({
		title: __("Prepare Autofattura"),
		fields: [
			{
				fieldname: "document_type",
				fieldtype: "Select",
				label: __("Document Type"),
				options: ["TD17", "TD18", "TD19"].join("\n"),
				default: context.autofattura_document_type || "TD17",
				reqd: 1,
			},
			{
				fieldname: "document_date",
				fieldtype: "Date",
				label: __("Autofattura Date"),
				default: context.autofattura_document_date || frm.doc.posting_date || frappe.datetime.nowdate(),
				reqd: 1,
			},
			{
				fieldname: "naming_series_info",
				fieldtype: "HTML",
				options: `<div class="text-muted">${__(
					"Autofattura documents will use naming series {0}.",
					[`<strong>${frappe.utils.escape_html(context.autofattura_naming_series || "")}</strong>`]
				)}</div>`,
			},
		],
		primary_action_label: context.autofattura_document ? __("Update Autofattura") : __("Prepare Autofattura"),
		primary_action: async (values) => {
			const response = await frappe.call({
				method: "fab_italy_edi.autofattura.prepare_autofattura_from_purchase_invoice",
				args: {
					docname: frm.doc.name,
					document_type: values.document_type,
					document_date: values.document_date,
				},
			});
			dialog.hide();
			frm._fab_autofattura_context = null;
			await frm.reload_doc();
			if (response.message?.autofattura) {
				open_autofattura_form(response.message.autofattura);
			}
		},
	});
	dialog.show();
}

function open_autofattura_form(docname) {
	remember_autofattura_workspace();
	frappe.set_route("Form", "Autofattura", docname);
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

function apply_supplier_route_options(frm, preview) {
	frm._fab_edi_supplier_route_options = preview?.supplier_route_options || {};
	const supplierDf = frm.get_docfield("supplier");
	if (supplierDf) {
		supplierDf.get_route_options_for_new_doc = () => frm._fab_edi_supplier_route_options || {};
	}
}

function clear_supplier_route_options(frm) {
	frm._fab_edi_supplier_route_options = {};
	const supplierDf = frm.get_docfield("supplier");
	if (supplierDf?.get_route_options_for_new_doc) {
		delete supplierDf.get_route_options_for_new_doc;
	}
}

function configure_supplier_new_doc_handler(frm) {
	const supplierControl = frm.fields_dict?.supplier;
	if (!supplierControl) {
		return;
	}

	if (!supplierControl._fab_edi_original_new_doc) {
		supplierControl._fab_edi_original_new_doc = supplierControl.new_doc?.bind(supplierControl);
	}

	supplierControl.new_doc = () => {
		supplierControl.$input && (supplierControl.$input._created_new_doc = true);
		create_supplier_from_edi(frm);
		return false;
	};
}

function reset_supplier_new_doc_handler(frm) {
	const supplierControl = frm.fields_dict?.supplier;
	if (!supplierControl?._fab_edi_original_new_doc) {
		return;
	}

	supplierControl.new_doc = supplierControl._fab_edi_original_new_doc;
}

async function create_supplier_from_edi(frm) {
	const supplierDoc = frappe.model.get_new_doc("Supplier", null, null, true);

	if (is_inbound_edi_purchase_invoice(frm)) {
		const preview = await get_inbound_invoice_preview(frm);
		apply_supplier_route_options(frm, preview);
		Object.assign(supplierDoc, build_supplier_quick_entry_doc(preview?.supplier || {}));
		Object.assign(supplierDoc, build_supplier_staging_doc(preview));
	} else {
		Object.assign(supplierDoc, build_manual_supplier_quick_entry_doc(frm));
	}

	frappe.ui.form.make_quick_entry(
		"Supplier",
		(createdDoc) => {
			const supplierName = createdDoc?.name || createdDoc?.doc?.name;
			if (supplierName) {
				frm.set_value("supplier", supplierName);
			}
		},
		null,
		supplierDoc
	);
}

function build_supplier_quick_entry_doc(supplier) {
	const routeOptions = {
		...(supplier || {}),
		...(supplier?.display_name ? { supplier_name: supplier.display_name } : {}),
		...(supplier?.province ? { state: supplier.province } : {}),
		...(supplier?.country ? { country_address: supplier.country } : {}),
		...(supplier?.email || supplier?.recipient_pec
			? { email_address: supplier.email || supplier.recipient_pec }
			: {}),
		...(supplier?.phone ? { mobile_number: supplier.phone } : {}),
	};

	delete routeOptions.display_name;
	delete routeOptions.province;
	delete routeOptions.recipient_pec;

	return routeOptions;
}

function build_supplier_staging_doc(preview) {
	return {
		fab_edi_supplier_preview_json: JSON.stringify(preview?.supplier || {}),
		fab_edi_payments_preview_json: JSON.stringify(preview?.payments || []),
	};
}

function build_manual_supplier_quick_entry_doc(frm) {
	const supplierControl = frm.fields_dict?.supplier;
	const supplierName =
		supplierControl?.get_input_value?.() ||
		supplierControl?.get_label_value?.() ||
		frm.doc.supplier_name ||
		"";

	return supplierName ? { supplier_name: supplierName } : {};
}
