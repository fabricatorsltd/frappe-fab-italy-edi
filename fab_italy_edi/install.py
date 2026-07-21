from __future__ import annotations

import json

import frappe
from frappe import _
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from erpnext.regional.italy import fiscal_regimes, vat_collectability_options

from fab_italy_edi.autofattura import backfill_autofatture
from fab_italy_edi.automation import ensure_automation_user
from fab_italy_edi.backends import get_provider_adapter
from fab_italy_edi.inbound_tax_setup import ensure_standard_inbound_natura_setup
from fab_italy_edi.purchase_invoice_import import (
	QUARANTINED_INBOUND_SUPPLIER_PREFIX,
	ensure_purchase_invoice_review_draft,
	is_quarantined_incoming_supplier_document,
)

LEGACY_INBOUND_DEMO_SAMPLES = (
	{
		"source_doctype": "Purchase Invoice",
		"source_name": "ACC-PINV-2026-00001",
		"external_submission_id": "377f3d43-d00c-4c54-bcaf-953bffb27937",
		"canonical_identifier": "IT12454611000_FRLGW.xml",
	},
)
LEGACY_EINVOICE_TYPE_DOCTYPE = "Tipologia di documento e-Invoice"
LEGACY_EINVOICE_TYPE_FIELDS = (
	("Sales Invoice", "custom_tipo_di_documento", "custom_descrizione_tipo_documento"),
	("Purchase Invoice", "custom_tipo_di_documento", "custom_descrizione_tipo_documento"),
)


def after_install():
	ensure_custom_fields()
	ensure_seed_records()
	ensure_standard_inbound_natura_setup()
	ensure_workspace_navigation()


def after_migrate():
	ensure_custom_fields()
	ensure_seed_records()
	ensure_standard_inbound_natura_setup()
	normalize_seeded_records()
	backfill_autofatture()


def ensure_custom_fields():
	create_custom_fields(get_custom_fields(), update=True)


def ensure_seed_records():
	ensure_automation_user()
	ensure_seed_documents("EDI Channel", "channel_key", get_default_channels())
	ensure_seed_documents("EDI Provider", "provider_name", get_default_providers())


def normalize_seeded_records():
	normalize_seeded_providers()
	ensure_workspace_navigation()
	neutralize_missing_legacy_einvoice_type_links()
	remove_inbound_invoices_page()
	quarantine_legacy_inbound_demo_samples()
	quarantine_stale_inbound_supplier_placeholders()
	backfill_inbound_purchase_invoice_drafts()


def ensure_seed_documents(doctype: str, lookup_field: str, documents: list[dict[str, object]]):
	for payload in documents:
		docname = frappe.db.get_value(doctype, {lookup_field: payload[lookup_field]})
		if not docname:
			frappe.get_doc({"doctype": doctype, **payload}).insert(ignore_permissions=True)
			continue

		doc = frappe.get_doc(doctype, docname)
		changed = False
		for fieldname, value in payload.items():
			if fieldname in {"doctype", "name"}:
				continue

			current_value = doc.get(fieldname)
			if current_value in (None, ""):
				doc.set(fieldname, value)
				changed = True

		if changed:
			doc.save(ignore_permissions=True)


def ensure_workspace_navigation():
	try:
		sidebar = frappe.get_doc("Workspace Sidebar", "Invoicing")
	except frappe.DoesNotExistError:
		sidebar = None

	if sidebar:
		remaining_items = [
			item
			for item in sidebar.items
			if not (item.type == "Link" and item.label == "Inbound Invoices")
		]
		if len(remaining_items) != len(sidebar.items):
			sidebar.items = remaining_items
			normalize_child_table_order(sidebar.items)
			save_standard_doc_without_export(sidebar)

	ensure_invoicing_autofatture_sidebar_link(sidebar)
	remove_autofatture_workspace_shortcut()


def ensure_invoicing_autofatture_sidebar_link(sidebar=None):
	if not sidebar:
		try:
			sidebar = frappe.get_doc("Workspace Sidebar", "Invoicing")
		except frappe.DoesNotExistError:
			return

	sidebar.items = [
		row
		for row in sidebar.items
		if not (row.type == "Link" and row.label == "Autofatture")
	]

	target_index = None
	for index, row in enumerate(sidebar.items):
		if row.type == "Section Break" and row.label == "Payments":
			target_index = index
			break

	new_row = sidebar.append(
		"items",
		{
			"type": "Link",
			"label": "Autofatture",
			"link_type": "DocType",
			"link_to": "Autofattura",
			"child": 1,
		},
	)
	if target_index is not None:
		sidebar.items.remove(new_row)
		sidebar.items.insert(target_index, new_row)

	normalize_child_table_order(sidebar.items)
	save_standard_doc_without_export(sidebar)


def remove_autofatture_workspace_shortcut():
	if not frappe.db.exists("Workspace", "Italy E-Invoicing"):
		return

	workspace = frappe.get_doc("Workspace", "Italy E-Invoicing")
	changed = False

	shortcuts = [row for row in workspace.shortcuts if row.label != "Autofatture"]
	if len(shortcuts) != len(workspace.shortcuts):
		workspace.shortcuts = shortcuts
		changed = True

	links = [row for row in workspace.links if not (row.type == "Link" and row.label == "Autofatture")]
	if len(links) != len(workspace.links):
		workspace.links = links
		changed = True

	content = json.loads(workspace.content or "[]")
	filtered_content = [
		block
		for block in content
		if not (
			block.get("type") == "shortcut"
			and block.get("data", {}).get("shortcut_name") == "Autofatture"
		)
	]
	if len(filtered_content) != len(content):
		workspace.content = json.dumps(filtered_content, separators=(",", ":"))
		changed = True

	if not changed:
		return

	normalize_child_table_order(workspace.shortcuts)
	normalize_child_table_order(workspace.links)
	workspace.save(ignore_permissions=True)


def remove_inbound_invoices_page():
	if not frappe.db.exists("Page", "inbound-invoices"):
		return

	frappe.delete_doc("Page", "inbound-invoices", ignore_permissions=True)


def neutralize_missing_legacy_einvoice_type_links():
	if frappe.db.exists("DocType", LEGACY_EINVOICE_TYPE_DOCTYPE):
		return

	for doctype, link_field, description_field in LEGACY_EINVOICE_TYPE_FIELDS:
		hide_legacy_custom_field(doctype, link_field)
		hide_legacy_custom_field(doctype, description_field)
		clear_legacy_field_values(doctype, link_field, description_field)


def scrub_missing_legacy_einvoice_type_link_values(document, method: str | None = None) -> None:
	if frappe.db.exists("DocType", LEGACY_EINVOICE_TYPE_DOCTYPE):
		return

	doctype = getattr(document, "doctype", None)
	for supported_doctype, link_field, description_field in LEGACY_EINVOICE_TYPE_FIELDS:
		if supported_doctype != doctype:
			continue

		changed = False
		if get_document_value(document, link_field):
			set_document_value(document, link_field, None)
			changed = True

		if description_field and get_document_value(document, description_field):
			set_document_value(document, description_field, None)
			changed = True

		if changed:
			break


def hide_legacy_custom_field(doctype: str, fieldname: str) -> None:
	custom_field_name = frappe.db.get_value("Custom Field", {"dt": doctype, "fieldname": fieldname}, "name")
	if not custom_field_name:
		return

	if frappe.db.get_value("Custom Field", custom_field_name, "hidden") == 1:
		return

	frappe.db.set_value("Custom Field", custom_field_name, "hidden", 1, update_modified=False)


def clear_legacy_field_values(doctype: str, link_field: str, description_field: str | None = None) -> None:
	fields = ["name", link_field]
	if description_field:
		fields.append(description_field)

	for row in frappe.get_all(doctype, fields=fields):
		updates = {}
		if row.get(link_field):
			updates[link_field] = None
		if description_field and row.get(description_field):
			updates[description_field] = None
		if updates:
			frappe.db.set_value(doctype, row["name"], updates, update_modified=False)


def get_document_value(document, fieldname: str):
	getter = getattr(document, "get", None)
	if callable(getter):
		return getter(fieldname)
	return getattr(document, fieldname, None)


def set_document_value(document, fieldname: str, value) -> None:
	setter = getattr(document, "set", None)
	if callable(setter):
		setter(fieldname, value)
		return

	setattr(document, fieldname, value)


def backfill_inbound_purchase_invoice_drafts():
	rows = frappe.get_all(
		"EDI Document",
		filters={"document_kind": "supplier_invoice_import"},
		fields=["name"],
	)
	for row in rows:
		document = frappe.get_doc("EDI Document", row["name"])
		if is_quarantined_incoming_supplier_document(document):
			continue
		try:
			ensure_purchase_invoice_review_draft(document)
		except Exception:
			frappe.log_error(
				title=_("Unable to backfill inbound Purchase Invoice draft"),
				message=frappe.get_traceback(),
			)


def quarantine_stale_inbound_supplier_placeholders():
	rows = frappe.get_all(
		"EDI Document",
		filters={"document_kind": "supplier_invoice_import"},
		fields=[
			"name",
			"company",
			"source_doctype",
			"source_name",
			"external_submission_id",
			"canonical_identifier",
		],
	)
	for row in rows:
		if row.get("source_doctype") != "EDI Configuration":
			continue

		collision = find_conflicting_edi_document(row)
		if not collision:
			continue

		document = frappe.get_doc("EDI Document", row["name"])
		if is_quarantined_incoming_supplier_document(document):
			continue

		previous_external_submission_id = document.external_submission_id
		document.validation_state = "invalid"
		document.transmission_state = "cancelled"
		document.latest_receipt_state = "cancelled"
		document.external_submission_id = None
		document.idempotency_key = f"quarantined:{document.name}"
		document.last_error = _(
			"{0}: collided with {1} {2}. Previous external submission ID: {3}."
		).format(
			QUARANTINED_INBOUND_SUPPLIER_PREFIX,
			collision.get("source_doctype") or collision.get("document_kind") or "EDI Document",
			collision.get("source_name") or collision.get("name") or "unknown",
			previous_external_submission_id or "unknown",
		)
		document.save(ignore_permissions=True)


def quarantine_legacy_inbound_demo_samples():
	rows = frappe.get_all(
		"EDI Document",
		filters={"document_kind": "supplier_invoice_import"},
		fields=[
			"name",
			"source_doctype",
			"source_name",
			"external_submission_id",
			"canonical_identifier",
		],
	)
	for row in rows:
		if not is_legacy_inbound_demo_sample(row):
			continue

		document = frappe.get_doc("EDI Document", row["name"])
		if is_quarantined_incoming_supplier_document(document):
			continue

		purchase_invoice_name = row.get("source_name") if row.get("source_doctype") == "Purchase Invoice" else None
		if purchase_invoice_name and frappe.db.exists("Purchase Invoice", purchase_invoice_name):
			frappe.db.set_value(
				"Purchase Invoice",
				purchase_invoice_name,
				{
					"fab_edi_document": None,
					"fab_edi_imported": 0,
					"fab_edi_source_xml": None,
				},
				update_modified=False,
			)

		previous_external_submission_id = document.external_submission_id
		document.validation_state = "invalid"
		document.transmission_state = "cancelled"
		document.latest_receipt_state = "cancelled"
		document.external_submission_id = None
		document.idempotency_key = f"quarantined:{document.name}"
		document.last_error = _(
			"{0}: legacy sandbox demo sample for {1}. Previous external submission ID: {2}."
		).format(
			QUARANTINED_INBOUND_SUPPLIER_PREFIX,
			purchase_invoice_name or document.name,
			previous_external_submission_id or "unknown",
		)
		document.save(ignore_permissions=True)


def is_legacy_inbound_demo_sample(row: dict[str, object]) -> bool:
	for sample in LEGACY_INBOUND_DEMO_SAMPLES:
		if all(row.get(fieldname) == value for fieldname, value in sample.items()):
			return True
	return False


def find_conflicting_edi_document(row) -> dict[str, object] | None:
	for candidate in frappe.get_all(
		"EDI Document",
		filters={"company": row["company"], "name": ["!=", row["name"]]},
		fields=[
			"name",
			"document_kind",
			"source_doctype",
			"source_name",
			"external_submission_id",
			"canonical_identifier",
		],
	):
		if candidate.get("document_kind") == "supplier_invoice_import":
			continue
		if row.get("external_submission_id") and candidate.get("external_submission_id") == row.get(
			"external_submission_id"
		):
			return candidate
		if row.get("canonical_identifier") and candidate.get("canonical_identifier") == row.get(
			"canonical_identifier"
		):
			return candidate
	return None


def normalize_child_table_order(rows):
	for idx, row in enumerate(rows, start=1):
		row.idx = idx


def save_standard_doc_without_export(doc):
	previous_in_import = getattr(frappe.flags, "in_import", False)
	frappe.flags.in_import = True
	try:
		doc.save(ignore_permissions=True)
	finally:
		frappe.flags.in_import = previous_in_import


def normalize_seeded_providers():
	for row in frappe.get_all("EDI Provider", fields=["name"]):
		doc = frappe.get_doc("EDI Provider", row["name"])
		adapter = get_provider_adapter(doc)
		normalizer = getattr(adapter, "normalize_provider_document", None) if adapter else None
		if callable(normalizer):
			before_state = (
				doc.environment,
				doc.auth_mode,
				doc.username,
				doc.endpoint_url,
				doc.status_url,
				json.dumps(coerce_json_mapping(doc.additional_settings), sort_keys=True),
			)
			normalizer(doc)
			after_state = (
				doc.environment,
				doc.auth_mode,
				doc.username,
				doc.endpoint_url,
				doc.status_url,
				json.dumps(coerce_json_mapping(doc.additional_settings), sort_keys=True),
			)
			if before_state != after_state:
				doc.save(ignore_permissions=True)


def coerce_json_mapping(value) -> dict[str, object]:
	if isinstance(value, dict):
		return dict(value)

	if isinstance(value, str):
		value = value.strip()
		if not value:
			return {}
		return json.loads(value)

	return {}


def get_custom_fields() -> dict[str, list[dict[str, object]]]:
	return {
		"Sales Invoice": get_sales_invoice_custom_fields(),
		"Purchase Invoice": get_purchase_invoice_custom_fields(),
		"Supplier": get_supplier_custom_fields(),
		"Company": get_company_custom_fields(),
		"Address": get_address_custom_fields(),
		"Customer": get_customer_custom_fields(),
	}


def get_company_custom_fields() -> list[dict[str, object]]:
	"""Italian identification fields read when building FatturaPA documents.

	ERPNext ships these under ``erpnext.regional.italy.setup``, but that setup only
	runs from the setup wizard and the v11/v12 patches, so a site that never went
	through an Italian setup wizard would not have them.
	"""
	return [
		{
			"fieldname": "sb_e_invoicing",
			"label": _("E-Invoicing"),
			"fieldtype": "Section Break",
			"insert_after": "date_of_establishment",
			"print_hide": 1,
		},
		{
			"fieldname": "fiscal_regime",
			"label": _("Fiscal Regime"),
			"fieldtype": "Select",
			"insert_after": "sb_e_invoicing",
			"print_hide": 1,
			"options": "\n".join(fiscal_regimes),
		},
		{
			"fieldname": "fiscal_code",
			"label": _("Fiscal Code"),
			"fieldtype": "Data",
			"insert_after": "fiscal_regime",
			"print_hide": 1,
			"description": _("Applicable if the company is an Individual or a Proprietorship"),
		},
		{
			"fieldname": "vat_collectability",
			"label": _("VAT Collectability"),
			"fieldtype": "Select",
			"insert_after": "fiscal_code",
			"print_hide": 1,
			"options": "\n".join(vat_collectability_options),
		},
	]


def get_address_custom_fields() -> list[dict[str, object]]:
	"""Province and country codes required by the FatturaPA address blocks.

	``state_code`` is populated by ``erpnext.regional.italy.utils.set_state_code``,
	which ERPNext already hooks on Address validate, so no hook is registered here.
	"""
	return [
		{
			"fieldname": "country_code",
			"label": _("Country Code"),
			"fieldtype": "Data",
			"insert_after": "country",
			"print_hide": 1,
			"fetch_from": "country.code",
		},
		{
			"fieldname": "state_code",
			"label": _("State Code"),
			"fieldtype": "Data",
			"insert_after": "state",
			"print_hide": 1,
		},
	]


def get_customer_custom_fields() -> list[dict[str, object]]:
	"""Recipient routing and identification fields for outbound e-invoices."""
	return [
		{
			"fieldname": "fiscal_code",
			"label": _("Fiscal Code"),
			"fieldtype": "Data",
			"insert_after": "tax_id",
			"print_hide": 1,
		},
		{
			"fieldname": "recipient_code",
			"label": _("Recipient Code"),
			"fieldtype": "Data",
			"insert_after": "fiscal_code",
			"print_hide": 1,
			"default": "0000000",
		},
		{
			"fieldname": "pec",
			"label": _("Recipient PEC"),
			"fieldtype": "Data",
			"insert_after": "recipient_code",
			"print_hide": 1,
		},
		{
			"fieldname": "is_public_administration",
			"label": _("Is Public Administration"),
			"fieldtype": "Check",
			"insert_after": "is_internal_customer",
			"print_hide": 1,
			"description": _("Set this if the customer is a Public Administration company."),
			"depends_on": 'eval:doc.customer_type=="Company"',
		},
	]


def get_default_channels() -> list[dict[str, object]]:
	return [
		{
			"channel_key": "sdi_proxy",
			"channel_name": "SDI via Proxy",
			"enabled": 1,
			"handler_path": "fab_italy_edi.channels.sdi_proxy.base.SDIProxyChannel",
			"supports_outbound": 1,
			"supports_inbound": 1,
			"supports_webhooks": 1,
			"supports_polling": 1,
			"capabilities": {
				"families": ["fatturapa"],
				"transport": "api",
			},
		},
		{
			"channel_key": "sdi_pec",
			"channel_name": "SDI via PEC",
			"enabled": 1,
			"handler_path": "fab_italy_edi.channels.sdi_pec.base.SDIPECChannel",
			"supports_outbound": 1,
			"supports_inbound": 1,
			"supports_webhooks": 0,
			"supports_polling": 1,
			"capabilities": {
				"families": ["fatturapa"],
				"transport": "mailbox",
			},
		},
		{
			"channel_key": "peppol",
			"channel_name": "PEPPOL",
			"enabled": 1,
			"handler_path": "fab_italy_edi.channels.peppol.base.PEPPOLChannel",
			"supports_outbound": 1,
			"supports_inbound": 1,
			"supports_webhooks": 1,
			"supports_polling": 1,
			"capabilities": {
				"families": ["peppol"],
				"transport": "network",
			},
		},
		{
			"channel_key": "nso",
			"channel_name": "NSO",
			"enabled": 1,
			"handler_path": "fab_italy_edi.channels.nso.base.NSOChannel",
			"supports_outbound": 1,
			"supports_inbound": 1,
			"supports_webhooks": 1,
			"supports_polling": 1,
			"capabilities": {
				"families": ["nso"],
				"transport": "network",
			},
		},
	]


def get_default_providers() -> list[dict[str, object]]:
	return [
		{
			"provider_name": "PEC Mailbox",
			"channel": "sdi_pec",
			"enabled": 1,
			"adapter_key": "sdi_pec",
			"auth_mode": "None",
			"environment": "Production",
			"use_environment_default_endpoint": 0,
			"endpoint_url": "",
			"status_url": "",
			"webhook_path": "",
			"use_webhooks": 0,
			"use_status_polling": 1,
			"additional_settings": {
				"recipient_address": "sdi01@pec.fatturapa.it",
				"imap_folder": "INBOX",
				"imap_search_criteria": "ALL",
			},
		},
		{
			"provider_name": "OpenAPI SDI Proxy",
			"channel": "sdi_proxy",
			"enabled": 1,
			"adapter_key": "openapi",
			"auth_mode": "Basic",
			"environment": "Production",
			"use_environment_default_endpoint": 1,
			"endpoint_url": "https://sdi.openapi.it",
			"status_url": "https://sdi.openapi.it",
			"webhook_path": "/api/method/fab_italy_edi.api.receive_openapi_callback",
			"use_webhooks": 0,
			"use_status_polling": 1,
			"additional_settings": {
				"connection_name": "SDI Production",
				"documentation_url": "https://console.openapi.com/it/apis/sdi/documentation",
				"openapi_spec_url": "https://console.openapi.com/oas/it/sdi.openapi.json",
				"oauth_token_url": "https://oauth.openapi.it/token",
				"account_auth_mode": "Basic",
				"transport_auth_mode": "Bearer Token",
				"customer_invoice_import_path": "/customer_invoice_imports",
				"invoices_path": "/invoices",
				"invoices_notifications_path": "/invoices_notifications",
				"business_registry_configuration_path": "/business_registry_configurations",
				"api_configuration_path": "/api_configurations",
			},
		}
	]


def get_sales_invoice_custom_fields() -> list[dict[str, object]]:
	return [
		{
			"fieldname": "fab_edi_section",
			"label": _("E-Invoicing"),
			"fieldtype": "Section Break",
			"insert_after": "select_print_heading",
		},
		{
			"fieldname": "fab_edi_document",
			"label": _("EDI Document"),
			"fieldtype": "Link",
			"options": "EDI Document",
			"insert_after": "fab_edi_section",
			"read_only": 1,
			"allow_on_submit": 1,
		},
		{
			"fieldname": "fab_edi_column_break",
			"fieldtype": "Column Break",
			"insert_after": "fab_edi_document",
		},
		{
			"fieldname": "fab_edi_transmission_state",
			"label": _("EDI Transmission State"),
			"fieldtype": "Data",
			"insert_after": "fab_edi_column_break",
			"read_only": 1,
			"allow_on_submit": 1,
			"in_standard_filter": 1,
		},
		{
			"fieldname": "fab_edi_receipt_state",
			"label": _("EDI Receipt State"),
			"fieldtype": "Data",
			"insert_after": "fab_edi_transmission_state",
			"read_only": 1,
			"allow_on_submit": 1,
		},
		{
			"fieldname": "fab_edi_cig",
			"label": _("CIG"),
			"fieldtype": "Data",
			"insert_after": "fab_edi_receipt_state",
			"allow_on_submit": 1,
		},
		{
			"fieldname": "fab_edi_cup",
			"label": _("CUP"),
			"fieldtype": "Data",
			"insert_after": "fab_edi_cig",
			"allow_on_submit": 1,
		},
	]


def get_purchase_invoice_custom_fields() -> list[dict[str, object]]:
	return [
		{
			"fieldname": "fab_edi_section",
			"label": _("E-Invoicing"),
			"fieldtype": "Section Break",
			"insert_after": "select_print_heading",
		},
		{
			"fieldname": "fab_edi_document",
			"label": _("EDI Document"),
			"fieldtype": "Link",
			"options": "EDI Document",
			"insert_after": "fab_edi_section",
			"read_only": 1,
			"allow_on_submit": 1,
		},
		{
			"fieldname": "fab_edi_column_break",
			"fieldtype": "Column Break",
			"insert_after": "fab_edi_document",
		},
		{
			"fieldname": "fab_edi_imported",
			"label": _("Imported from EDI"),
			"fieldtype": "Check",
			"insert_after": "fab_edi_column_break",
			"read_only": 1,
			"allow_on_submit": 1,
		},
		{
			"fieldname": "fab_edi_source_xml",
			"label": _("Source XML"),
			"fieldtype": "Attach",
			"insert_after": "fab_edi_imported",
			"read_only": 1,
			"allow_on_submit": 1,
		},
		{
			"fieldname": "fab_edi_cig",
			"label": _("CIG"),
			"fieldtype": "Data",
			"insert_after": "fab_edi_source_xml",
			"allow_on_submit": 1,
		},
		{
			"fieldname": "fab_edi_cup",
			"label": _("CUP"),
			"fieldtype": "Data",
			"insert_after": "fab_edi_cig",
			"allow_on_submit": 1,
		},
		{
			"fieldname": "fab_edi_autofattura_document",
			"label": _("Autofattura EDI Document"),
			"fieldtype": "Link",
			"options": "EDI Document",
			"insert_after": "fab_edi_cup",
			"read_only": 1,
			"allow_on_submit": 1,
		},
		{
			"fieldname": "fab_edi_autofattura",
			"label": _("Autofattura"),
			"fieldtype": "Link",
			"options": "Autofattura",
			"insert_after": "fab_edi_autofattura_document",
			"read_only": 1,
			"allow_on_submit": 1,
		},
	]


def get_supplier_custom_fields() -> list[dict[str, object]]:
	return [
		{
			"fieldname": "fab_edi_supplier_preview_json",
			"label": _("FAB EDI Supplier Preview"),
			"fieldtype": "Small Text",
			"insert_after": "default_bank_account",
			"hidden": 1,
			"read_only": 1,
			"no_copy": 1,
		},
		{
			"fieldname": "fab_edi_payments_preview_json",
			"label": _("FAB EDI Payments Preview"),
			"fieldtype": "Small Text",
			"insert_after": "fab_edi_supplier_preview_json",
			"hidden": 1,
			"read_only": 1,
			"no_copy": 1,
		},
	]
