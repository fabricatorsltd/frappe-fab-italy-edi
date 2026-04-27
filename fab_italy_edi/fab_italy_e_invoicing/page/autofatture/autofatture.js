const AUTOFATTURA_WORKSPACE_STORAGE_KEY = "fab_italy_edi.autofattura_workspace";

frappe.provide("fab_italy_edi");

frappe.pages["autofatture"].on_page_load = function (wrapper) {
	fab_italy_edi.autofatture_page = new fab_italy_edi.AutofatturePage(wrapper);
};

frappe.pages["autofatture"].on_page_show = function () {
	fab_italy_edi.autofatture_page?.refresh();
};

fab_italy_edi.AutofatturePage = class AutofatturePage {
	constructor(wrapper) {
		this.wrapper = $(wrapper);
		this.page = frappe.ui.make_app_page({
			parent: wrapper,
			title: __("Autofatture"),
			single_column: true,
		});
		this.page.set_primary_action(__("Refresh"), () => this.refresh());
		this.container = $('<div class="fab-autofatture-page"></div>').appendTo(this.page.main);
		this.bind_events();
	}

	bind_events() {
		this.container.on("click", "[data-action]", (event) => {
			const { action, docname, sourceName, documentType, documentDate } = event.currentTarget.dataset;

			if (action === "show-autofattura" && docname) {
				frappe.route_options = { autofattura_document: docname };
				frappe.set_route("autofatture");
				return;
			}

			if (action === "open-technical-record" && docname) {
				frappe.set_route("Form", "EDI Document", docname);
				return;
			}

			if (action === "open-autofattura-editor" && docname) {
				remember_autofattura_workspace();
				frappe.set_route("Form", "Autofattura", docname);
				return;
			}

			if (action === "open-purchase-invoice" && sourceName) {
				frappe.set_route("Form", "Purchase Invoice", sourceName);
				return;
			}

			if (action === "edit-autofattura" && sourceName) {
				this.openEditDialog({ sourceName, documentType, documentDate });
				return;
			}

			if (action === "confirm-review" && docname) {
				void this.confirmReview(docname);
				return;
			}

			if (action === "send-autofattura" && docname) {
				void this.sendAutofattura(docname);
			}
		});
	}

	async confirmReview(docname) {
		const response = await frappe.call({
			method: "fab_italy_edi.autofattura.confirm_autofattura_review",
			args: { docname },
			freeze: true,
			freeze_message: __("Confirming autofattura review..."),
		});
		frappe.show_alert({ message: __("Autofattura review confirmed"), indicator: "green" });
		frappe.route_options = { autofattura_document: response.message?.autofattura || docname };
		await this.refresh();
	}

	async sendAutofattura(docname) {
		const response = await frappe.call({
			method: "fab_italy_edi.autofattura.send_autofattura_to_sdi",
			args: { docname },
			freeze: true,
			freeze_message: __("Queueing autofattura for SDI..."),
		});
		const message = response.message || {};
		if (message.last_error) {
			frappe.show_alert({ message: __("Autofattura send failed"), indicator: "orange" });
		} else {
			frappe.show_alert({ message: __("Autofattura queued to SDI"), indicator: "green" });
		}
		frappe.route_options = { autofattura_document: message.autofattura || docname };
		await this.refresh();
	}

	openEditDialog({ sourceName, documentType, documentDate }) {
		const dialog = new frappe.ui.Dialog({
			title: __("Update Autofattura Draft"),
			fields: [
				{
					fieldname: "document_type",
					fieldtype: "Select",
					label: __("Document Type"),
					options: ["TD17", "TD18", "TD19"].join("\n"),
					default: documentType || "TD17",
					reqd: 1,
				},
				{
					fieldname: "document_date",
					fieldtype: "Date",
					label: __("Autofattura Date"),
					default: documentDate || frappe.datetime.nowdate(),
					reqd: 1,
				},
			],
			primary_action_label: __("Save"),
			primary_action: async (values) => {
				const response = await frappe.call({
					method: "fab_italy_edi.autofattura.prepare_autofattura_from_purchase_invoice",
					args: {
						docname: sourceName,
						document_type: values.document_type,
						document_date: values.document_date,
					},
				});
				dialog.hide();
				frappe.route_options = { autofattura_document: response.message?.autofattura };
				await this.refresh();
			},
		});
		dialog.show();
	}

	async refresh() {
		const selectedDocname = frappe.route_options?.autofattura_document || null;
		if (frappe.route_options?.autofattura_document) {
			delete frappe.route_options.autofattura_document;
		}

		this.container.html(`<div class="text-muted">${__("Loading autofatture...")}</div>`);
		const response = await frappe.call({
			method: "fab_italy_edi.autofattura.get_autofattura_dashboard",
			args: { docname: selectedDocname },
		});
		this.data = response.message || {};
		this.render();
	}

	render() {
		const selected = this.data.selected_autofattura;
		const rows = Array.isArray(this.data.autofatture) ? this.data.autofatture : [];
		const otherRows = selected ? rows.filter((row) => row.name !== selected.name) : rows;
		const showTable = !selected || otherRows.length > 0;
		this.container.html(`
			<div class="row">
				<div class="col-md-12">${build_selected_autofattura_html(selected)}</div>
			</div>
			${showTable
				? `<div class="row" style="margin-top: 20px;">
					<div class="col-md-12">${build_autofattura_table_html(otherRows, selected ? __("Other Prepared Autofatture") : __("Prepared Autofatture"))}</div>
				</div>`
				: ""}
		`);
	}
};

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

function build_selected_autofattura_html(selected) {
	if (!selected) {
		return `
			<div class="alert alert-info">
				<strong>${__("Autofatture")}</strong><br>
				${__(
					"This page is the business-facing view for prepared autofattura records. Choose one from the list below to review the source Purchase Invoice, taxonomy code meaning, and current draft status."
				)}
			</div>`;
	}

	return `
		<div class="frappe-card">
			<div class="frappe-card-body" style="padding: 20px;">
			<div class="row">
				<div class="col-sm-8">
					<h4 style="margin-top: 0;">${escape_html(selected.document_type_label || selected.autofattura_document_type || __("Autofattura"))}</h4>
					<p style="margin-bottom: 8px;">${escape_html(selected.document_type_help || "")}</p>
					<p class="text-muted" style="margin-bottom: 0;">
						${escape_html(selected.party_name || "")} ·
						${__("Purchase Invoice")} ${escape_html(selected.source_name || "")} ·
						${__("Date")} ${escape_html(selected.autofattura_document_date || "")}
					</p>
				</div>
				<div class="col-sm-4">
					<p style="margin-bottom: 8px;"><strong>${__("Review")}</strong>: ${getReviewStateLabel(selected)}</p>
					<p style="margin-bottom: 8px;"><strong>${__("Transmission")}</strong>: ${escape_html(selected.transmission_state || "")}</p>
					<p style="margin-bottom: 8px;"><strong>${__("Document Number")}</strong>: ${escape_html(selected.autofattura_document_number || "—")}</p>
					<p style="margin-bottom: 0;"><strong>${__("Internal Record")}</strong>: ${escape_html(selected.name || "")}</p>
				</div>
			</div>
			<div style="margin-top: 12px; display: flex; gap: 8px; flex-wrap: wrap;">
				${buildConfirmButtonHtml(selected)}
				${buildSendButtonHtml(selected)}
				<button class="btn btn-default btn-sm" data-action="open-purchase-invoice" data-source-name="${escape_html(selected.source_name || "")}">
					${__("Open Purchase Invoice")}
				</button>
				<button
					class="btn btn-default btn-sm"
					data-action="edit-autofattura"
					data-source-name="${escape_html(selected.source_name || "")}"
					data-document-type="${escape_html(selected.autofattura_document_type || "")}"
					data-document-date="${escape_html(selected.autofattura_document_date || "")}">
					${__("Edit Draft Settings")}
				</button>
				<button class="btn btn-default btn-sm" data-action="open-autofattura-editor" data-docname="${escape_html(selected.name || "")}">
					${__("Open Autofattura Editor")}
				</button>
			</div>
			<div class="text-muted small" style="margin-top: 12px;">
				${escape_html(selected.send_help || "")}
			</div>
			${build_selected_preview_html(selected)}
			</div>
		</div>`;
}

function build_selected_preview_html(selected) {
	const source = selected?.source_purchase_invoice;
	if (!source) {
		return "";
	}

	const notices = Array.isArray(selected.preview_notices) ? selected.preview_notices : [];
	const sendBlockers = Array.isArray(selected.send_blockers) ? selected.send_blockers : [];
	const editorLines = Array.isArray(selected.autofattura_lines) ? selected.autofattura_lines : [];
	const editorTaxSummaries = Array.isArray(selected.autofattura_tax_summaries)
		? selected.autofattura_tax_summaries
		: [];
	const noticeHtml = notices.length
		? `<div style="margin-top: 16px; display: grid; gap: 8px;">${notices
				.map((notice) => buildNoticeHtml(notice, selected))
				.join("")}</div>`
		: "";
	const sendBlockerHtml = sendBlockers.length
		? `<div class="alert alert-warning" style="margin-top: 16px; margin-bottom: 0;">
				<strong>${__("Send blockers")}</strong>
				<ul style="margin: 8px 0 0 16px;">
					${sendBlockers.map((message) => `<li>${escape_html(message)}</li>`).join("")}
				</ul>
			</div>`
		: `<div class="alert alert-success" style="margin-top: 16px; margin-bottom: 0;">
				${__("This autofattura is ready to be sent to SDI.")}
			</div>`;
	const editorLineRows = editorLines.length
		? editorLines
				.map(
					(row) => `
						<tr>
							<td>${escape_html(row.description || "")}</td>
							<td class="text-right">${formatValue(row.qty)}</td>
							<td>${escape_html(row.uom || "")}</td>
							<td class="text-right">${formatMoney(row.unit_price, selected.autofattura_currency || source.currency)}</td>
							<td class="text-right">${formatMoney(row.total_price, selected.autofattura_currency || source.currency)}</td>
							<td class="text-right">${row.tax_rate == null ? "—" : formatValue(row.tax_rate)}</td>
							<td>${escape_html(row.nature || "")}</td>
						</tr>`
				)
				.join("")
		: `<tr><td colspan="7">${__("No autofattura lines defined yet.")}</td></tr>`;
	const editorTaxRows = editorTaxSummaries.length
		? editorTaxSummaries
				.map(
					(row) => `
						<tr>
							<td class="text-right">${formatValue(row.rate)}</td>
							<td class="text-right">${formatMoney(row.taxable_amount, selected.autofattura_currency || source.currency)}</td>
							<td class="text-right">${formatMoney(row.tax_amount, selected.autofattura_currency || source.currency)}</td>
							<td>${escape_html(row.nature || "")}</td>
							<td>${escape_html(row.law || "")}</td>
						</tr>`
				)
				.join("")
		: `<tr><td colspan="5">${__("No VAT summaries generated yet.")}</td></tr>`;

	const itemRows = (source.items || []).length
		? source.items
				.map(
					(row) => `
						<tr>
							<td>${escape_html(row.item_name || "")}</td>
							<td>${escape_html(row.description || "")}</td>
							<td class="text-right">${formatValue(row.qty)}</td>
							<td>${escape_html(row.uom || "")}</td>
							<td class="text-right">${formatMoney(row.rate, source.currency)}</td>
							<td class="text-right">${formatMoney(row.amount, source.currency)}</td>
							<td>${escape_html(row.expense_account || "")}</td>
							<td>${escape_html(row.item_tax_template || "")}</td>
						</tr>`
				)
				.join("")
		: `<tr><td colspan="8">${__("No source item rows found.")}</td></tr>`;

	const taxRows = (source.taxes || []).length
		? source.taxes
				.map(
					(row) => `
						<tr>
							<td>${escape_html(row.description || "")}</td>
							<td>${escape_html(row.account_head || "")}</td>
							<td>${escape_html(row.charge_type || "")}</td>
							<td class="text-right">${formatValue(row.rate)}</td>
							<td class="text-right">${formatMoney(row.tax_amount, source.currency)}</td>
						</tr>`
				)
				.join("")
		: `<tr><td colspan="5">${__("No source tax rows found.")}</td></tr>`;

	return `
		<div class="frappe-card" style="margin-top: 16px;">
			<div class="frappe-card-head">
				<strong>${__("Source Purchase Invoice Preview")}</strong>
			</div>
			<div class="frappe-card-body" style="padding-top: 12px;">
				<div class="row">
					<div class="col-sm-6">
						<p>
							<strong>${__("Supplier")}:</strong> ${escape_html(selected.party_name || "")}<br>
							<strong>${__("Source Purchase Invoice")}:</strong> ${escape_html(selected.source_name || "")}<br>
							<strong>${__("Supplier Invoice Number")}:</strong> ${escape_html(selected.autofattura_reference_invoice_number || source.bill_no || "")}<br>
							<strong>${__("Posting Date")}:</strong> ${escape_html(source.posting_date || "")}<br>
							<strong>${__("Supplier Invoice Date")}:</strong> ${escape_html(source.bill_date || "")}<br>
							<strong>${__("Currency")}:</strong> ${escape_html(source.currency || "")}<br>
							<strong>${__("Payable Account")}:</strong> ${escape_html(source.credit_to || "")}
						</p>
					</div>
					<div class="col-sm-6">
						<p>
							<strong>${__("Autofattura Date")}:</strong> ${escape_html(selected.autofattura_document_date || "")}<br>
							<strong>${__("Autofattura Number")}:</strong> ${escape_html(selected.autofattura_document_number || "—")}<br>
							<strong>${__("Autofattura Currency")}:</strong> ${escape_html(selected.autofattura_currency || source.currency || "")}<br>
							<strong>${__("Naming Series")}:</strong> ${escape_html(selected.autofattura_naming_series || "")}<br>
							<strong>${__("Company")}:</strong> ${escape_html(selected.company || "")}<br>
							<strong>${__("Autofattura Net Total")}:</strong> ${formatMoney(selected.autofattura_net_total, selected.autofattura_currency || source.currency)}<br>
							<strong>${__("Autofattura Tax Total")}:</strong> ${formatMoney(selected.autofattura_tax_total, selected.autofattura_currency || source.currency)}<br>
							<strong>${__("Autofattura Grand Total")}:</strong> ${formatMoney(selected.autofattura_grand_total, selected.autofattura_currency || source.currency)}<br>
							<strong>${__("Source Purchase Total")}:</strong> ${formatMoney(source.grand_total, source.currency)}
						</p>
					</div>
				</div>
				${noticeHtml}
				${sendBlockerHtml}
				<h5 style="margin-top: 16px;">${__("Autofattura Editor Lines")}</h5>
				<table class="table table-bordered">
					<thead>
						<tr>
							<th>${__("Description")}</th>
							<th class="text-right">${__("Qty")}</th>
							<th>${__("UOM")}</th>
							<th class="text-right">${__("Unit Price")}</th>
							<th class="text-right">${__("Net Amount")}</th>
							<th class="text-right">${__("VAT %")}</th>
							<th>${__("Nature")}</th>
						</tr>
					</thead>
					<tbody>${editorLineRows}</tbody>
				</table>
				<h5 style="margin-top: 16px;">${__("Derived VAT Summary")}</h5>
				<table class="table table-bordered">
					<thead>
						<tr>
							<th class="text-right">${__("VAT %")}</th>
							<th class="text-right">${__("Taxable Amount")}</th>
							<th class="text-right">${__("Tax Amount")}</th>
							<th>${__("Nature")}</th>
							<th>${__("Reference Law")}</th>
						</tr>
					</thead>
					<tbody>${editorTaxRows}</tbody>
				</table>
				<h5 style="margin-top: 16px;">${__("Source Item Rows")}</h5>
				<table class="table table-bordered">
					<thead>
						<tr>
							<th>${__("Item")}</th>
							<th>${__("Description")}</th>
							<th class="text-right">${__("Qty")}</th>
							<th>${__("UOM")}</th>
							<th class="text-right">${__("Rate")}</th>
							<th class="text-right">${__("Amount")}</th>
							<th>${__("Expense Account")}</th>
							<th>${__("Item Tax Template")}</th>
						</tr>
					</thead>
					<tbody>${itemRows}</tbody>
				</table>
				<h5 style="margin-top: 16px;">${__("Source Tax Rows")}</h5>
				<table class="table table-bordered">
					<thead>
						<tr>
							<th>${__("Description")}</th>
							<th>${__("Account")}</th>
							<th>${__("Charge Type")}</th>
							<th class="text-right">${__("Rate %")}</th>
							<th class="text-right">${__("Tax Amount")}</th>
						</tr>
					</thead>
					<tbody>${taxRows}</tbody>
				</table>
			</div>
		</div>`;
}

function build_autofattura_table_html(rows, title) {
	const body = rows.length
		? rows
				.map(
					(row) => `
						<tr>
							<td>${escape_html(row.document_type_label || row.autofattura_document_type || "")}</td>
							<td>${escape_html(row.party_name || "")}</td>
							<td>${escape_html(row.source_name || "")}</td>
							<td>${escape_html(row.autofattura_document_date || "")}</td>
							<td>${escape_html(row.company || "")}</td>
							<td>${escape_html(row.validation_state || "")}</td>
							<td>${escape_html(row.transmission_state || "")}</td>
							<td class="text-right">
								<button class="btn btn-xs btn-default" data-action="show-autofattura" data-docname="${escape_html(row.name || "")}">
									${__("Review")}
								</button>
							</td>
						</tr>`
				)
				.join("")
		: `<tr><td colspan="8">${__("No autofatture prepared yet.")}</td></tr>`;

	return `
		<div class="frappe-card">
			<div class="frappe-card-head">
				<strong>${escape_html(title || __("Prepared Autofatture"))}</strong>
			</div>
			<div class="frappe-card-body" style="padding-top: 12px;">
				<table class="table table-bordered">
					<thead>
						<tr>
							<th>${__("Type")}</th>
							<th>${__("Supplier")}</th>
							<th>${__("Source Purchase Invoice")}</th>
							<th>${__("Date")}</th>
							<th>${__("Company")}</th>
							<th>${__("Validation")}</th>
							<th>${__("Transmission")}</th>
							<th style="width: 100px;"></th>
						</tr>
					</thead>
					<tbody>${body}</tbody>
				</table>
			</div>
		</div>`;
}

function escape_html(value) {
	return frappe.utils.escape_html(value == null ? "" : String(value));
}

function buildNoticeHtml(notice, selected) {
	const level = notice?.level === "warning" ? "warning" : "info";
	const actionHtml = buildNoticeActionHtml(notice, selected);

	return `
		<div class="alert alert-${level}" style="margin-bottom: 0; display: flex; justify-content: space-between; gap: 12px; align-items: center;">
			<div>${escape_html(notice?.message || "")}</div>
			${actionHtml}
		</div>`;
}

function buildNoticeActionHtml(notice, selected) {
	if (notice?.action === "open_purchase_invoice") {
		return `<button class="btn btn-default btn-sm" data-action="open-purchase-invoice" data-source-name="${escape_html(selected?.source_name || "")}">
			${__("Open Purchase Invoice")}
		</button>`;
	}

	if (notice?.action === "edit_autofattura") {
		return `<button
			class="btn btn-default btn-sm"
			data-action="edit-autofattura"
			data-source-name="${escape_html(selected?.source_name || "")}"
			data-document-type="${escape_html(selected?.autofattura_document_type || "")}"
			data-document-date="${escape_html(selected?.autofattura_document_date || "")}">
			${__("Edit Draft Settings")}
		</button>`;
	}

	if (notice?.action === "open_autofattura_editor") {
		return `<button class="btn btn-default btn-sm" data-action="open-autofattura-editor" data-docname="${escape_html(selected?.name || "")}">
			${__("Open Autofattura Editor")}
		</button>`;
	}

	return "";
}

function buildConfirmButtonHtml(selected) {
	if (selected?.validation_state === "valid") {
		return `<button class="btn btn-primary btn-sm" disabled>${__("Review Confirmed")}</button>`;
	}

	const disabled = selected?.can_confirm_review ? "" : "disabled";
	const title = selected?.can_confirm_review
		? __("Confirm Review")
		: __("Resolve the remaining review items first");
	return `<button class="btn btn-primary btn-sm" data-action="confirm-review" data-docname="${escape_html(selected?.name || "")}" ${disabled}>
		${escape_html(title)}
	</button>`;
}

function buildSendButtonHtml(selected) {
	const activeStates = new Set(["queued", "sending", "sent", "delivered", "accepted"]);
	if (activeStates.has(selected?.transmission_state)) {
		return `<button class="btn btn-success btn-sm" disabled>${__("Already Sent to SDI")}</button>`;
	}

	const disabled = selected?.can_send ? "" : "disabled";
	const title =
		selected?.transmission_state === "failed" || selected?.transmission_state === "rejected"
			? __("Retry SDI Send")
			: __("Send to SDI");
	return `<button class="btn btn-success btn-sm" data-action="send-autofattura" data-docname="${escape_html(selected?.name || "")}" ${disabled}>
		${escape_html(title)}
	</button>`;
}

function getReviewStateLabel(selected) {
	if (selected?.validation_state === "valid") {
		return __("Confirmed");
	}

	if (selected?.can_confirm_review) {
		return __("Ready for confirmation");
	}

	return __("Needs changes");
}

function formatValue(value) {
	return frappe.format(Number(value || 0), { fieldtype: "Float" });
}

function formatMoney(value, currency = null) {
	return format_currency(
		normalizeNumericValue(value),
		currency || frappe.boot.sysdefaults.currency
	);
}

function normalizeNumericValue(value) {
	if (typeof value === "number") {
		return value;
	}

	if (typeof value !== "string") {
		return Number(value || 0);
	}

	const trimmed = value.trim();
	if (!trimmed) {
		return 0;
	}

	const compact = trimmed.replace(/\s+/g, "");
	if (/^-?\d{1,3}(\.\d{3})*,\d+$/.test(compact)) {
		return Number(compact.replace(/\./g, "").replace(",", "."));
	}

	if (/^-?\d+,\d+$/.test(compact)) {
		return Number(compact.replace(",", "."));
	}

	if (/^-?\d{1,3}(,\d{3})+\.\d+$/.test(compact)) {
		return Number(compact.replace(/,/g, ""));
	}

	return Number(compact);
}
