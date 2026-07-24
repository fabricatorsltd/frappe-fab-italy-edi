const FAB_EDI_ITALY_COUNTRY_NAMES = new Set([
	"Italy",
	"Italia",
	"Italian Republic",
	"Repubblica Italiana",
]);
const FAB_EDI_SENT_STATES = new Set(["queued", "sent", "delivered", "accepted"]);
const FAB_EDI_SUCCESS_STATES = new Set(["delivered", "accepted"]);
const FAB_EDI_ATTENTION_STATES = new Set(["rejected", "failed", "cancelled"]);
const FAB_EDI_SDI_DEADLINE_DAYS = 12;

frappe.ui.form.on("Sales Invoice", {
	refresh(frm) {
		void update_fab_edi_sdi_notice(frm);
	},
});

async function update_fab_edi_sdi_notice(frm) {
	const refresh_token = `${frm.doc.name || "new"}:${frm.doc.modified || "0"}`;
	frm.__fab_edi_notice_refresh_token = refresh_token;

	if (!should_show_fab_edi_notice(frm)) {
		clear_fab_edi_notice(frm);
		return;
	}

	const company_country = await get_company_country(frm.doc.company);
	if (frm.__fab_edi_notice_refresh_token !== refresh_token) {
		return;
	}

	if (!FAB_EDI_ITALY_COUNTRY_NAMES.has(company_country)) {
		clear_fab_edi_notice(frm);
		return;
	}

	const edi_status = await get_fab_edi_status(frm);
	if (frm.__fab_edi_notice_refresh_token !== refresh_token) {
		return;
	}

	const transmission_state = (edi_status?.transmission_state || frm.doc.fab_edi_transmission_state || "")
		.trim()
		.toLowerCase();
	const receipt_state = (edi_status?.latest_receipt_state || frm.doc.fab_edi_receipt_state || "")
		.trim()
		.toLowerCase();
	const deadline_context = get_sdi_deadline_context(frm.doc.posting_date);

	let message;
	let color;
	if (edi_status || transmission_state || receipt_state) {
		({ message, color } = get_fab_edi_status_message({
			transmission_state,
			receipt_state,
			transmission_date: frm.doc.fab_edi_transmission_date,
			external_submission_id: edi_status?.external_submission_id,
			last_error: edi_status?.last_error,
		}, deadline_context));
	} else {
		({ message, color } = get_fab_edi_notice_message(deadline_context));
	}

	frm.dashboard.clear_headline();
	frm.dashboard.set_headline_alert(message, color, true);
	add_fab_edi_actions(frm, transmission_state);
}

function add_fab_edi_actions(frm, transmission_state) {
	add_fab_edi_document_button(frm);
	if (!can_send_to_sdi(transmission_state)) {
		return;
	}

	frm.add_custom_button(
		frm.doc.fab_edi_document ? __("Retry SDI Send") : __("Send to SDI"),
		() => send_sales_invoice_to_sdi(frm),
		__("FAB EDI")
	);
}

function add_fab_edi_document_button(frm) {
	if (!frm.doc.fab_edi_document) {
		return;
	}

	frm.add_custom_button(
		__("EDI Document"),
		() => frappe.set_route("Form", "EDI Document", frm.doc.fab_edi_document),
		__("FAB EDI")
	);
}

function should_show_fab_edi_notice(frm) {
	return Boolean(frm.doc.docstatus === 1 && frm.doc.company && frm.doc.posting_date);
}

async function get_company_country(company) {
	if (!company) {
		return null;
	}

	const response = await frappe.db.get_value("Company", company, "country");
	return response.message?.country || null;
}

async function get_fab_edi_status(frm) {
	if (!frm.doc.fab_edi_document) {
		return null;
	}

	const response = await frappe.db.get_value("EDI Document", frm.doc.fab_edi_document, [
		"transmission_state",
		"latest_receipt_state",
		"external_submission_id",
		"last_error",
	]);
	return response.message || null;
}

function clear_fab_edi_notice(frm) {
	frm.dashboard.clear_headline();
}

function get_sdi_deadline_context(posting_date) {
	const deadline = frappe.datetime.add_days(posting_date, FAB_EDI_SDI_DEADLINE_DAYS);
	const days_remaining = frappe.datetime.get_diff(deadline, frappe.datetime.get_today());

	return {
		deadline,
		deadline_label: frappe.datetime.str_to_user(deadline, false, true),
		days_remaining,
	};
}

function get_deadline_status_text(days_remaining) {
	if (days_remaining < 0) {
		const overdue_days = Math.abs(days_remaining);
		return overdue_days === 1 ? __("1 day overdue") : __("{0} days overdue", [overdue_days]);
	}

	if (days_remaining === 0) {
		return __("due today");
	}

	if (days_remaining === 1) {
		return __("1 day remaining");
	}

	return __("{0} days remaining", [days_remaining]);
}

function get_fab_edi_notice_message(deadline_context) {
	return {
		message: get_deadline_message(deadline_context),
		color: get_deadline_color(deadline_context),
	};
}

function get_fab_edi_status_message(status, deadline_context) {
	const transmission_state = status.transmission_state || "";
	const receipt_state = status.receipt_state || "";
	const primary_state = receipt_state || transmission_state || "draft";
	const parts = [__("SDI status: {0}", [format_edi_state(primary_state)])];

	if (receipt_state && transmission_state && receipt_state !== transmission_state) {
		parts.push(__("Transmission: {0}", [format_edi_state(transmission_state)]));
	}

	if (status.external_submission_id) {
		parts.push(__("ID: {0}", [status.external_submission_id]));
	}

	if (FAB_EDI_ATTENTION_STATES.has(primary_state) && status.last_error) {
		parts.push(first_line(status.last_error));
	}

	// once the invoice reached SDI the 12 day transmission deadline is met:
	// show when it left, never the countdown
	if (FAB_EDI_SENT_STATES.has(primary_state)) {
		parts.push(get_success_confirmation_message(primary_state, status.transmission_date));
		return {
			message: parts.join(" · "),
			color: get_edi_state_color(primary_state),
		};
	}

	parts.push(get_deadline_message(deadline_context));

	return {
		message: parts.join(" · "),
		color: get_highest_priority_color(
			get_edi_state_color(primary_state),
			get_deadline_color(deadline_context)
		),
	};
}

function get_deadline_message(deadline_context) {
	const deadline_status = get_deadline_status_text(deadline_context.days_remaining);
	return __("SDI deadline: {0} ({1})", [deadline_context.deadline_label, deadline_status]);
}

function get_success_confirmation_message(state, transmission_date) {
	const sent_on = transmission_date
		? __("transmitted on {0}", [frappe.datetime.str_to_user(transmission_date, false, true)])
		: __("transmitted to SDI");

	if (state === "delivered") {
		return __("Delivered to the recipient ({0}).", [sent_on]);
	}

	if (state === "accepted") {
		return __("Confirmed with a final positive outcome ({0}).", [sent_on]);
	}

	// queued / sent: reached SDI in time, awaiting the recipient receipt
	return sent_on.charAt(0).toUpperCase() + sent_on.slice(1) + ".";
}

function get_deadline_color(deadline_context) {
	return deadline_context.days_remaining < 0 ? "red" : "yellow";
}

function get_edi_state_color(state) {
	if (FAB_EDI_ATTENTION_STATES.has(state)) {
		return "red";
	}

	if (FAB_EDI_SUCCESS_STATES.has(state)) {
		return "green";
	}

	// transmitted, waiting for the recipient receipt: informational, not a warning
	if (FAB_EDI_SENT_STATES.has(state)) {
		return "blue";
	}

	return "yellow";
}

function get_highest_priority_color(...colors) {
	const priorities = {
		red: 3,
		orange: 2,
		yellow: 2,
		green: 1,
		blue: 1,
		gray: 0,
		grey: 0,
	};

	return colors.reduce((selected, candidate) => {
		const selectedPriority = priorities[selected] ?? 0;
		const candidatePriority = priorities[candidate] ?? 0;
		return candidatePriority > selectedPriority ? candidate : selected;
	}, "gray");
}

function format_edi_state(state) {
	return (state || "draft")
		.split("_")
		.map((part) => part.charAt(0).toUpperCase() + part.slice(1))
		.join(" ");
}

function first_line(text) {
	return String(text || "").split("\n")[0];
}

function can_send_to_sdi(transmission_state) {
	return !FAB_EDI_SENT_STATES.has(transmission_state) && transmission_state !== "sending";
}

function send_sales_invoice_to_sdi(frm) {
	frappe.call({
		method: "fab_italy_edi.api.send_sales_invoice_to_sdi",
		args: {
			docname: frm.doc.name,
		},
		freeze: true,
		freeze_message: __("Queueing invoice for SDI..."),
		callback(r) {
			if (!r.message) {
				return;
			}

			frm.doc.fab_edi_document = r.message.edi_document || frm.doc.fab_edi_document;
			frm.doc.fab_edi_transmission_state =
				r.message.transmission_state || frm.doc.fab_edi_transmission_state;
			frm.doc.fab_edi_receipt_state =
				r.message.latest_receipt_state || frm.doc.fab_edi_receipt_state;
			frm.refresh();
			void frm.reload_doc();

			if (r.message.transmission_state === "failed") {
				frappe.msgprint({
					title: __("SDI send failed"),
					message: r.message.last_error || __("The SDI proxy rejected the invoice."),
					indicator: "red",
				});
				return;
			}

			const message = r.message.external_submission_id
				? __("Queued for SDI as {0}", [r.message.external_submission_id])
				: __("Queued for SDI");
			frappe.show_alert({ message, indicator: "green" });
		},
		error() {
			void frm.reload_doc();
		},
	});
}
