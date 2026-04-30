import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

from fab_italy_edi import inbound_tax_setup
from fab_italy_edi import install


APP_ROOT = Path(__file__).resolve().parents[1]


class TestInstall(unittest.TestCase):
	def test_save_standard_doc_without_export_restores_flag(self):
		doc = SimpleNamespace(save=Mock())
		frappe_stub = SimpleNamespace(flags=SimpleNamespace(in_import=False))

		with patch.object(install, "frappe", new=frappe_stub):
			install.save_standard_doc_without_export(doc)

		doc.save.assert_called_once_with(ignore_permissions=True)
		self.assertFalse(frappe_stub.flags.in_import)

	def test_custom_fields_cover_sales_and_purchase_invoices(self):
		with patch.object(install, "_", side_effect=lambda text: text):
			custom_fields = install.get_custom_fields()

		self.assertEqual(set(custom_fields), {"Sales Invoice", "Purchase Invoice", "Supplier"})
		self.assertGreaterEqual(
			{field["fieldname"] for field in custom_fields["Sales Invoice"]},
			{
				"fab_edi_section",
				"fab_edi_document",
				"fab_edi_transmission_state",
				"fab_edi_receipt_state",
				"fab_edi_cig",
				"fab_edi_cup",
			},
		)
		self.assertGreaterEqual(
			{field["fieldname"] for field in custom_fields["Purchase Invoice"]},
			{
				"fab_edi_section",
				"fab_edi_document",
				"fab_edi_imported",
				"fab_edi_source_xml",
				"fab_edi_cig",
				"fab_edi_cup",
				"fab_edi_autofattura_document",
				"fab_edi_autofattura",
			},
		)
		self.assertGreaterEqual(
			{field["fieldname"] for field in custom_fields["Supplier"]},
			{
				"fab_edi_supplier_preview_json",
				"fab_edi_payments_preview_json",
			},
		)

	def test_default_seed_data_contains_openapi_provider(self):
		channels = {item["channel_key"]: item for item in install.get_default_channels()}
		providers = {item["provider_name"]: item for item in install.get_default_providers()}

		self.assertIn("sdi_proxy", channels)
		self.assertIn("sdi_pec", channels)
		self.assertIn("PEC Mailbox", providers)
		self.assertIn("OpenAPI SDI Proxy", providers)
		self.assertEqual(providers["PEC Mailbox"]["adapter_key"], "sdi_pec")
		self.assertEqual(providers["PEC Mailbox"]["channel"], "sdi_pec")
		self.assertEqual(
			providers["PEC Mailbox"]["additional_settings"]["recipient_address"],
			"sdi01@pec.fatturapa.it",
		)
		self.assertEqual(providers["OpenAPI SDI Proxy"]["adapter_key"], "openapi")
		self.assertEqual(providers["OpenAPI SDI Proxy"]["auth_mode"], "Basic")
		self.assertEqual(providers["OpenAPI SDI Proxy"]["environment"], "Production")
		self.assertEqual(providers["OpenAPI SDI Proxy"]["endpoint_url"], "https://sdi.openapi.it")
		self.assertEqual(
			providers["OpenAPI SDI Proxy"]["additional_settings"]["oauth_token_url"],
			"https://oauth.openapi.it/token",
		)
		self.assertEqual(providers["OpenAPI SDI Proxy"]["additional_settings"]["account_auth_mode"], "Basic")
		self.assertEqual(providers["OpenAPI SDI Proxy"]["additional_settings"]["transport_auth_mode"], "Bearer Token")

	def test_edi_document_doctype_contains_operational_tables(self):
		doctype_path = (
			APP_ROOT
			/ "fab_italy_edi"
			/ "fab_italy_e_invoicing"
			/ "doctype"
			/ "edi_document"
			/ "edi_document.json"
		)

		doctype = json.loads(doctype_path.read_text())
		fields = {field["fieldname"]: field for field in doctype["fields"]}

		self.assertEqual(fields["transmission_attempts"]["options"], "EDI Transmission Attempt")
		self.assertEqual(fields["receipts"]["options"], "EDI Receipt")
		self.assertEqual(fields["source_name"]["fieldtype"], "Dynamic Link")
		self.assertEqual(fields["autofattura_document_type"]["fieldtype"], "Select")
		self.assertEqual(fields["autofattura_document_date"]["fieldtype"], "Date")
		self.assertEqual(fields["autofattura_document_number"]["fieldtype"], "Data")
		self.assertEqual(fields["autofattura_naming_series"]["fieldtype"], "Data")
		self.assertEqual(fields["autofattura_currency"]["fieldtype"], "Link")
		self.assertEqual(fields["autofattura_lines"]["fieldtype"], "Table")
		self.assertEqual(fields["autofattura_lines"]["options"], "EDI Autofattura Line")

	def test_autofattura_doctype_contains_business_fields(self):
		doctype_path = (
			APP_ROOT
			/ "fab_italy_edi"
			/ "fab_italy_e_invoicing"
			/ "doctype"
			/ "autofattura"
			/ "autofattura.json"
		)

		doctype = json.loads(doctype_path.read_text())
		fields = {field["fieldname"]: field for field in doctype["fields"]}

		self.assertEqual(fields["source_purchase_invoice"]["options"], "Purchase Invoice")
		self.assertEqual(fields["linked_edi_document"]["options"], "EDI Document")
		self.assertEqual(fields["document_type"]["fieldtype"], "Select")
		self.assertEqual(fields["document_date"]["fieldtype"], "Date")
		self.assertEqual(fields["document_number"]["fieldtype"], "Data")
		self.assertEqual(fields["supplier_invoice_number"]["fieldtype"], "Data")
		self.assertEqual(fields["currency"]["options"], "Currency")
		self.assertEqual(fields["lines"]["fieldtype"], "Table")
		self.assertEqual(fields["lines"]["options"], "EDI Autofattura Line")
		self.assertEqual(fields["overview_section"]["fieldtype"], "Section Break")
		self.assertEqual(fields["column_break_overview"]["fieldtype"], "Column Break")
		self.assertEqual(fields["column_break_document"]["fieldtype"], "Column Break")
		self.assertEqual(fields["column_break_totals"]["fieldtype"], "Column Break")
		self.assertEqual(fields["transport_section"]["fieldtype"], "Section Break")
		self.assertEqual(fields["transport_section"]["collapsible"], 1)
		self.assertEqual(fields["supplier_name"]["hidden"], 1)
		self.assertEqual(fields["generated_xml"]["depends_on"], "eval:doc.generated_xml")
		self.assertEqual(fields["last_error"]["depends_on"], "eval:doc.last_error")

	def test_edi_configuration_doctype_contains_automatic_refresh_fields(self):
		doctype_path = (
			APP_ROOT
			/ "fab_italy_edi"
			/ "fab_italy_e_invoicing"
			/ "doctype"
			/ "edi_configuration"
			/ "edi_configuration.json"
		)

		doctype = json.loads(doctype_path.read_text())
		fields = {field["fieldname"]: field for field in doctype["fields"]}

		self.assertEqual(fields["auto_refresh_enabled"]["fieldtype"], "Check")
		self.assertEqual(fields["outbound_status_polling_interval_mins"]["default"], "15")
		self.assertEqual(fields["incoming_invoice_polling_interval_mins"]["default"], "60")
		self.assertEqual(fields["notification_polling_interval_mins"]["default"], "15")
		self.assertEqual(fields["last_outbound_status_polling_at"]["fieldtype"], "Datetime")
		self.assertEqual(fields["inbound_tax_mappings"]["fieldtype"], "Table")
		self.assertEqual(fields["inbound_tax_mappings"]["options"], "EDI Inbound Tax Mapping")
		self.assertEqual(fields["autofattura_naming_series"]["fieldtype"], "Data")

	def test_standard_inbound_natura_catalog_contains_expected_codes(self):
		codes = {row["nature"] for row in inbound_tax_setup.get_standard_inbound_natura_tax_types()}
		self.assertEqual(
			codes,
			{
				"N1",
				"N2",
				"N2.1",
				"N2.2",
				"N3",
				"N3.1",
				"N3.2",
				"N3.3",
				"N3.4",
				"N3.5",
				"N3.6",
				"N4",
				"N5",
				"N6",
				"N6.1",
				"N6.2",
				"N6.3",
				"N6.4",
				"N6.5",
				"N6.6",
				"N6.7",
				"N6.8",
				"N6.9",
				"N7",
			},
		)

	def test_append_missing_standard_inbound_natura_mappings_keeps_existing_rows(self):
		class StubConfiguration(SimpleNamespace):
			def get(self, fieldname):
				return getattr(self, fieldname, None)

			def append(self, fieldname, value):
				getattr(self, fieldname).append(value)

		account_heads = {
			row["nature"]: f"{row['nature']} Account - fab"
			for row in inbound_tax_setup.get_standard_inbound_natura_tax_types()
		}
		configuration = StubConfiguration(
			company="Fabricators",
			inbound_tax_mappings=[
				{"tax_rate": 0.0, "nature": "N2.2", "account_head": "Custom N2.2 - fab"},
			],
		)

		changed = inbound_tax_setup.append_missing_standard_inbound_natura_mappings(
			configuration,
			account_heads=account_heads,
		)

		self.assertTrue(changed)
		self.assertEqual(
			len(configuration.inbound_tax_mappings),
			len(inbound_tax_setup.get_standard_inbound_natura_tax_types()),
		)
		n22_rows = [row for row in configuration.inbound_tax_mappings if row["nature"] == "N2.2"]
		self.assertEqual(n22_rows, [{"tax_rate": 0.0, "nature": "N2.2", "account_head": "Custom N2.2 - fab"}])

	def test_enable_disabled_inbound_natura_account_only_for_standard_nature(self):
		frappe_stub = SimpleNamespace(
			db=SimpleNamespace(
				get_value=Mock(return_value=1),
				set_value=Mock(),
			)
		)

		with patch.object(inbound_tax_setup, "frappe", new=frappe_stub):
			result = inbound_tax_setup.ensure_inbound_natura_account_enabled(
				"VAT Natura N2.2 - fab",
				nature="N2.2",
			)

		self.assertEqual(result, "VAT Natura N2.2 - fab")
		frappe_stub.db.set_value.assert_called_once_with(
			"Account",
			"VAT Natura N2.2 - fab",
			"disabled",
			0,
			update_modified=False,
		)

	def test_quarantine_stale_inbound_supplier_placeholders_clears_live_identifier(self):
		stale_document = SimpleNamespace(
			name="EDI-STALE-0001",
			document_kind="supplier_invoice_import",
			external_submission_id="dup-uuid",
			idempotency_key="incoming:dup-uuid",
			validation_state="not_validated",
			transmission_state="ready",
			latest_receipt_state="delivered",
			last_error=None,
			save=Mock(),
		)
		frappe_stub = SimpleNamespace(
			get_all=Mock(
				side_effect=[
					[
						{
							"name": "EDI-STALE-0001",
							"company": "Fabricators",
							"source_doctype": "EDI Configuration",
							"source_name": "fabricators",
							"external_submission_id": "dup-uuid",
							"canonical_identifier": "IT001.xml",
						}
					],
					[
						{
							"name": "EDI-OUT-0001",
							"document_kind": "invoice",
							"source_doctype": "Sales Invoice",
							"source_name": "ACC-SINV-2026-00001",
							"external_submission_id": "dup-uuid",
							"canonical_identifier": "IT001.xml",
						}
					],
				]
			),
			get_doc=Mock(return_value=stale_document),
		)

		with (
			patch.object(install, "frappe", new=frappe_stub),
			patch.object(install, "_", side_effect=lambda text: text),
		):
			install.quarantine_stale_inbound_supplier_placeholders()

		self.assertIsNone(stale_document.external_submission_id)
		self.assertEqual(stale_document.idempotency_key, "quarantined:EDI-STALE-0001")
		self.assertEqual(stale_document.validation_state, "invalid")
		self.assertEqual(stale_document.transmission_state, "cancelled")
		self.assertEqual(stale_document.latest_receipt_state, "cancelled")
		self.assertIn("Quarantined stale inbound supplier placeholder", stale_document.last_error)
		stale_document.save.assert_called_once_with(ignore_permissions=True)

	def test_quarantine_legacy_inbound_demo_samples_unlinks_purchase_invoice(self):
		legacy_document = SimpleNamespace(
			name="EDI-DEMO-0001",
			document_kind="supplier_invoice_import",
			external_submission_id="377f3d43-d00c-4c54-bcaf-953bffb27937",
			idempotency_key="incoming:377f3d43-d00c-4c54-bcaf-953bffb27937",
			validation_state="not_validated",
			transmission_state="ready",
			latest_receipt_state="delivered",
			last_error=None,
			save=Mock(),
		)
		frappe_stub = SimpleNamespace(
			get_all=Mock(
				return_value=[
					{
						"name": "EDI-DEMO-0001",
						"source_doctype": "Purchase Invoice",
						"source_name": "ACC-PINV-2026-00001",
						"external_submission_id": "377f3d43-d00c-4c54-bcaf-953bffb27937",
						"canonical_identifier": "IT12454611000_FRLGW.xml",
					}
				]
			),
			get_doc=Mock(return_value=legacy_document),
			db=SimpleNamespace(
				exists=Mock(return_value=True),
				set_value=Mock(),
			),
		)

		with (
			patch.object(install, "frappe", new=frappe_stub),
			patch.object(install, "_", side_effect=lambda text: text),
		):
			install.quarantine_legacy_inbound_demo_samples()

		frappe_stub.db.set_value.assert_called_once_with(
			"Purchase Invoice",
			"ACC-PINV-2026-00001",
			{
				"fab_edi_document": None,
				"fab_edi_imported": 0,
				"fab_edi_source_xml": None,
			},
			update_modified=False,
		)
		self.assertIsNone(legacy_document.external_submission_id)
		self.assertEqual(legacy_document.idempotency_key, "quarantined:EDI-DEMO-0001")
		self.assertEqual(legacy_document.validation_state, "invalid")
		self.assertEqual(legacy_document.transmission_state, "cancelled")
		self.assertEqual(legacy_document.latest_receipt_state, "cancelled")
		self.assertIn("Quarantined stale inbound supplier placeholder", legacy_document.last_error)
		self.assertIn("ACC-PINV-2026-00001", legacy_document.last_error)
		legacy_document.save.assert_called_once_with(ignore_permissions=True)

	def test_neutralize_missing_legacy_einvoice_type_links_hides_fields_and_clears_values(self):
		def mock_exists(doctype, name):
			if doctype == "DocType" and name == install.LEGACY_EINVOICE_TYPE_DOCTYPE:
				return False
			return True

		def mock_get_value(doctype, filters, fieldname=None, **kwargs):
			if doctype != "Custom Field":
				return None
			if isinstance(filters, str):
				is_link_field = "tipo_di_documento" in filters and "descrizione" not in filters
				field_values = {
					"hidden": 0,
					"reqd": 1 if is_link_field else 0,
					"fieldtype": "Link" if is_link_field else "Data",
					"options": "Tipologia di documento e-Invoice" if is_link_field else "",
				}
				return field_values.get(fieldname)
			return {
				("Sales Invoice", "custom_tipo_di_documento"): "Sales Invoice-custom_tipo_di_documento_per_fatt_elett",
				("Sales Invoice", "custom_descrizione_tipo_documento"): "Sales Invoice-custom_descrizione_tipo_documento",
				("Purchase Invoice", "custom_tipo_di_documento"): "Purchase Invoice-custom_tipo_di_documento",
				("Purchase Invoice", "custom_descrizione_tipo_documento"): "Purchase Invoice-custom_descrizione_tipo_documento",
			}.get((filters["dt"], filters["fieldname"]))

		frappe_stub = SimpleNamespace(
			db=SimpleNamespace(
				exists=Mock(side_effect=mock_exists),
				get_value=Mock(side_effect=mock_get_value),
				set_value=Mock(),
			),
			get_all=Mock(
				side_effect=[
					[
						{
							"name": "ACC-SINV-2026-00001",
							"custom_tipo_di_documento": "customer.custom_tipo_fattura_elettronica",
							"custom_descrizione_tipo_documento": "Fattura",
						}
					],
					[
						{
							"name": "ACC-PINV-2026-00001",
							"custom_tipo_di_documento": "supplier.custom_tipo_autofattura",
							"custom_descrizione_tipo_documento": "Autofattura",
						}
					],
				]
			),
		)

	def test_scrub_missing_legacy_einvoice_type_link_values_clears_document_fields(self):
		document = SimpleNamespace(
			doctype="Sales Invoice",
			custom_tipo_di_documento="customer.custom_tipo_fattura_elettronica",
			custom_descrizione_tipo_documento="Fattura",
		)

		def mock_exists(doctype, name):
			if doctype == "DocType" and name == install.LEGACY_EINVOICE_TYPE_DOCTYPE:
				return False
			return True

		frappe_stub = SimpleNamespace(db=SimpleNamespace(exists=Mock(side_effect=mock_exists)))

		with patch.object(install, "frappe", new=frappe_stub):
			install.scrub_missing_legacy_einvoice_type_link_values(document)

		self.assertIsNone(document.custom_tipo_di_documento)
		self.assertIsNone(document.custom_descrizione_tipo_documento)

		def mock_get_value(doctype, filters, fieldname=None, **kwargs):
			if doctype != "Custom Field":
				return None
			if isinstance(filters, str):
				is_link_field = "tipo_di_documento" in filters and "descrizione" not in filters
				field_values = {
					"hidden": 0,
					"reqd": 1 if is_link_field else 0,
					"fieldtype": "Link" if is_link_field else "Data",
					"options": "Tipologia di documento e-Invoice" if is_link_field else "",
				}
				return field_values.get(fieldname)
			return {
				("Sales Invoice", "custom_tipo_di_documento"): "Sales Invoice-custom_tipo_di_documento_per_fatt_elett",
				("Sales Invoice", "custom_descrizione_tipo_documento"): "Sales Invoice-custom_descrizione_tipo_documento",
				("Purchase Invoice", "custom_tipo_di_documento"): "Purchase Invoice-custom_tipo_di_documento",
				("Purchase Invoice", "custom_descrizione_tipo_documento"): "Purchase Invoice-custom_descrizione_tipo_documento",
			}.get((filters["dt"], filters["fieldname"]))

		neutralize_stub = SimpleNamespace(
			db=SimpleNamespace(
				exists=Mock(side_effect=mock_exists),
				get_value=Mock(side_effect=mock_get_value),
				set_value=Mock(),
			),
			get_all=Mock(
				side_effect=[
					[
						{
							"name": "ACC-SINV-2026-00001",
							"custom_tipo_di_documento": "customer.custom_tipo_fattura_elettronica",
							"custom_descrizione_tipo_documento": "Fattura",
						}
					],
					[
						{
							"name": "ACC-PINV-2026-00001",
							"custom_tipo_di_documento": "supplier.custom_tipo_autofattura",
							"custom_descrizione_tipo_documento": "Autofattura",
						}
					],
				]
			),
		)

		with patch.object(install, "frappe", new=neutralize_stub):
			install.neutralize_missing_legacy_einvoice_type_links()

		neutralize_stub.db.set_value.assert_has_calls(
			[
				call(
					"Custom Field",
					"Sales Invoice-custom_tipo_di_documento_per_fatt_elett",
					{"hidden": 1, "reqd": 0, "fieldtype": "Data", "options": ""},
					update_modified=False,
				),
				call(
					"Custom Field",
					"Sales Invoice-custom_descrizione_tipo_documento",
					{"hidden": 1},
					update_modified=False,
				),
				call(
					"Custom Field",
					"Purchase Invoice-custom_tipo_di_documento",
					{"hidden": 1, "reqd": 0, "fieldtype": "Data", "options": ""},
					update_modified=False,
				),
				call(
					"Custom Field",
					"Purchase Invoice-custom_descrizione_tipo_documento",
					{"hidden": 1},
					update_modified=False,
				),
				call(
					"Sales Invoice",
					"ACC-SINV-2026-00001",
					{
						"custom_tipo_di_documento": None,
						"custom_descrizione_tipo_documento": None,
					},
					update_modified=False,
				),
				call(
					"Purchase Invoice",
					"ACC-PINV-2026-00001",
					{
						"custom_tipo_di_documento": None,
						"custom_descrizione_tipo_documento": None,
					},
					update_modified=False,
				),
			]
			,
			any_order=True,
		)
