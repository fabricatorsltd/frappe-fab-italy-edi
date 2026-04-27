import unittest
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

import frappe

from fab_italy_edi import autofattura
from fab_italy_edi.fatturapa import autofattura as autofattura_xml


class FakeEDIDocument:
	def __init__(self, **kwargs):
		self.transmission_attempts = []
		self.save_calls = 0
		self.__dict__.update(kwargs)

	def save(self, ignore_permissions=False):
		self.save_calls += 1
		return self

	def append(self, fieldname, value):
		getattr(self, fieldname).append(SimpleNamespace(**value))


class TestAutofattura(unittest.TestCase):
	def test_build_autofattura_context_requires_sequence_for_foreign_supplier(self):
		purchase_invoice = SimpleNamespace(name="PINV-0001", company="Fabricators", supplier="SUP-0001")
		frappe_stub = SimpleNamespace(
			db=SimpleNamespace(exists=Mock(return_value=True)),
			get_cached_value=Mock(
				side_effect=lambda doctype, name, fieldname: {
					("Company", "Fabricators", "country"): "Italy",
					("Supplier", "SUP-0001", "tax_id"): "IE1234567A",
					("Purchase Invoice", "PINV-0001", "fab_edi_autofattura"): None,
					("Purchase Invoice", "PINV-0001", "fab_edi_autofattura_document"): None,
					("EDI Configuration", "Fabricators", "autofattura_naming_series"): None,
				}.get((doctype, name, fieldname))
			),
		)

		with (
			patch.object(autofattura, "frappe", new=frappe_stub),
			patch.object(autofattura, "resolve_supplier_country", return_value="Ireland"),
			patch.object(autofattura, "get_existing_autofattura_name", return_value=None),
			patch.object(autofattura, "get_existing_autofattura_document_name", return_value=None),
		):
			context = autofattura.build_autofattura_context(purchase_invoice)

		self.assertTrue(context["is_foreign_supplier"])
		self.assertTrue(context["requires_naming_series"])
		self.assertFalse(context["can_prepare"])
		self.assertIn("Autofattura Naming Series", context["reason"])

	def test_prepare_autofattura_from_purchase_invoice_creates_linked_business_document(self):
		purchase_invoice = SimpleNamespace(
			name="PINV-0001",
			company="Fabricators",
			supplier="SUP-0001",
			supplier_name="Atlassian Pty Ltd",
			bill_no="INV-001",
			bill_date="2026-04-29",
			currency="USD",
			items=[
				SimpleNamespace(
					description="Monthly subscription",
					item_name="Jira, Standard",
					item_code="JIRA-STANDARD",
					qty=1,
					uom="Month",
					stock_uom="Month",
					net_rate=36.2,
					rate=36.2,
					net_amount=36.2,
					amount=36.2,
					item_tax_rate=None,
					item_tax_template=None,
				)
			],
			add_comment=Mock(),
		)
		autofattura_doc = SimpleNamespace(
			name="AUTO-0001",
			save=Mock(),
			validation_state=None,
			transmission_state=None,
			latest_receipt_state=None,
			supplier_invoice_number=None,
			supplier_invoice_date=None,
			currency=None,
			lines=[],
		)
		transport_doc = FakeEDIDocument(name="EDI-AUTO-0001")
		frappe_stub = SimpleNamespace(
			new_doc=Mock(return_value=autofattura_doc),
			get_doc=Mock(),
			db=SimpleNamespace(set_value=Mock(), exists=Mock(return_value=True)),
		)

		with (
			patch.object(autofattura, "frappe", new=frappe_stub),
			patch.object(autofattura, "get_purchase_invoice_for_autofattura", return_value=purchase_invoice),
			patch.object(
				autofattura,
				"build_autofattura_context",
				return_value={
					"is_company_in_italy": True,
					"supplier": "SUP-0001",
					"is_foreign_supplier": True,
					"autofattura": None,
					"autofattura_document": None,
					"autofattura_document_date": None,
					"autofattura_naming_series": "AUTO-.YYYY.-",
				},
			),
			patch.object(autofattura, "get_or_create_autofattura_transport_document", return_value=transport_doc),
			patch.object(autofattura, "sync_transport_document_from_autofattura"),
			patch.object(
				autofattura,
				"sync_autofattura_from_transport_document",
				side_effect=lambda doc, transport, save=False: setattr(doc, "linked_edi_document", transport.name) or doc,
			),
		):
			result = autofattura.prepare_autofattura_from_purchase_invoice("PINV-0001", "TD17", "2026-04-30")

		self.assertEqual(result["autofattura"], "AUTO-0001")
		self.assertEqual(result["edi_document"], "EDI-AUTO-0001")
		self.assertEqual(autofattura_doc.document_type, "TD17")
		self.assertEqual(autofattura_doc.document_date, "2026-04-30")
		self.assertEqual(autofattura_doc.naming_series, "AUTO-.YYYY.-")
		self.assertEqual(autofattura_doc.supplier_invoice_number, "INV-001")
		self.assertEqual(autofattura_doc.supplier_invoice_date, "2026-04-29")
		self.assertEqual(autofattura_doc.currency, "USD")
		self.assertEqual(len(autofattura_doc.lines), 1)
		self.assertEqual(autofattura_doc.lines[0].description, "Monthly subscription")
		autofattura_doc.save.assert_called_once_with(ignore_permissions=True)
		self.assertEqual(transport_doc.save_calls, 1)
		frappe_stub.db.set_value.assert_has_calls(
			[
				call(
					"Purchase Invoice",
					"PINV-0001",
					"fab_edi_autofattura",
					"AUTO-0001",
					update_modified=False,
				),
				call(
					"Purchase Invoice",
					"PINV-0001",
					"fab_edi_autofattura_document",
					"EDI-AUTO-0001",
					update_modified=False,
				),
			]
		)

	def test_sync_purchase_invoice_links_from_autofattura_updates_purchase_invoice(self):
		frappe_stub = SimpleNamespace(
			db=SimpleNamespace(
				exists=Mock(return_value=True),
				set_value=Mock(),
			)
		)
		autofattura_doc = SimpleNamespace(
			name="AUTO-0001",
			source_purchase_invoice="PINV-0001",
			linked_edi_document="EDI-AUTO-0001",
		)

		with patch.object(autofattura, "frappe", new=frappe_stub):
			autofattura.sync_purchase_invoice_links_from_autofattura(autofattura_doc)

		frappe_stub.db.set_value.assert_has_calls(
			[
				call(
					"Purchase Invoice",
					"PINV-0001",
					"fab_edi_autofattura",
					"AUTO-0001",
					update_modified=False,
				),
				call(
					"Purchase Invoice",
					"PINV-0001",
					"fab_edi_autofattura_document",
					"EDI-AUTO-0001",
					update_modified=False,
				),
			]
		)

	def test_prepare_autofattura_from_purchase_invoice_blocks_non_foreign_supplier(self):
		with (
			patch.object(
				autofattura,
				"get_purchase_invoice_for_autofattura",
				return_value=SimpleNamespace(name="PINV-0001", company="Fabricators", supplier="SUP-0001"),
			),
			patch.object(
				autofattura,
				"build_autofattura_context",
				return_value={
					"is_company_in_italy": True,
					"supplier": "SUP-0001",
					"is_foreign_supplier": False,
					"autofattura": None,
					"autofattura_document": None,
					"autofattura_naming_series": "AUTO-.YYYY.-",
					"reason": "Set a foreign supplier country first.",
				},
			),
		):
			with self.assertRaises(frappe.ValidationError):
				autofattura.prepare_autofattura_from_purchase_invoice("PINV-0001", "TD17")

	def test_confirm_autofattura_review_marks_document_valid(self):
		autofattura_doc = SimpleNamespace(
			name="AUTO-0001",
			source_purchase_invoice="PINV-0001",
			validation_state="draft",
			save=Mock(),
		)
		transport_doc = FakeEDIDocument(name="EDI-AUTO-0001", validation_state="draft")
		frappe_stub = SimpleNamespace(has_permission=Mock())

		with (
			patch.object(autofattura, "frappe", new=frappe_stub),
			patch.object(autofattura, "get_autofattura", return_value=autofattura_doc),
			patch.object(autofattura, "get_autofattura_review_notices", return_value=[]),
			patch.object(autofattura, "get_or_create_autofattura_transport_document", return_value=transport_doc),
			patch.object(autofattura, "sync_transport_document_from_autofattura"),
			patch.object(
				autofattura,
				"sync_autofattura_from_transport_document",
				side_effect=lambda doc, transport, save=False: doc,
			),
		):
			result = autofattura.confirm_autofattura_review("AUTO-0001")

		self.assertEqual(result["validation_state"], "valid")
		self.assertEqual(autofattura_doc.validation_state, "valid")
		self.assertEqual(transport_doc.validation_state, "valid")
		self.assertEqual(transport_doc.save_calls, 1)

	def test_sync_autofattura_from_transport_document_refreshes_before_save(self):
		stale_autofattura = SimpleNamespace(
			name="AUTO-0001",
			doctype="Autofattura",
			linked_edi_document=None,
			validation_state="draft",
			transmission_state="draft",
			latest_receipt_state=None,
			generated_xml=None,
			last_error=None,
			document_number=None,
			save=Mock(),
		)
		fresh_autofattura = SimpleNamespace(
			name="AUTO-0001",
			doctype="Autofattura",
			linked_edi_document=None,
			validation_state="draft",
			transmission_state="draft",
			latest_receipt_state=None,
			generated_xml=None,
			last_error=None,
			document_number=None,
			save=Mock(),
		)
		transport_doc = SimpleNamespace(
			name="EDI-AUTO-0001",
			validation_state="valid",
			transmission_state="queued",
			latest_receipt_state="queued",
			generated_xml="/private/files/autofattura.xml",
			last_error=None,
			autofattura_document_number="AUTO-2026-00001",
		)
		frappe_stub = SimpleNamespace(
			db=SimpleNamespace(exists=Mock(return_value=True)),
			get_doc=Mock(return_value=fresh_autofattura),
		)

		with patch.object(autofattura, "frappe", new=frappe_stub):
			result = autofattura.sync_autofattura_from_transport_document(
				stale_autofattura,
				transport_doc,
				save=True,
			)

		self.assertIs(result, fresh_autofattura)
		fresh_autofattura.save.assert_called_once_with(ignore_permissions=True)
		stale_autofattura.save.assert_not_called()
		self.assertEqual(stale_autofattura.linked_edi_document, "EDI-AUTO-0001")
		self.assertEqual(stale_autofattura.transmission_state, "queued")
		self.assertEqual(fresh_autofattura.document_number, "AUTO-2026-00001")

	def test_get_autofattura_dashboard_returns_business_labels(self):
		selected_document = SimpleNamespace(
			name="AUTO-0001",
			source_purchase_invoice="PINV-0001",
			linked_edi_document="EDI-AUTO-0001",
			company="Fabricators",
			supplier="SUP-0001",
			supplier_name="Atlassian Pty Ltd",
			document_type="TD17",
			document_date="2026-04-25",
			document_number=None,
			naming_series="AUTO-.YYYY.-",
			supplier_invoice_number="INV-001",
			supplier_invoice_date="2026-04-19",
			currency="USD",
			validation_state="draft",
			transmission_state="draft",
			latest_receipt_state=None,
			generated_xml=None,
			last_error=None,
			lines=[
				SimpleNamespace(
					description="Monthly subscription",
					quantity=1,
					unit_of_measure="Month",
					unit_price=36.2,
					total_price=36.2,
					tax_rate=22,
					nature=None,
					reference_law=None,
				)
			],
			modified="2026-04-25 15:00:00",
		)
		frappe_stub = SimpleNamespace(
			get_doc=Mock(return_value=selected_document),
			has_permission=Mock(),
			get_cached_value=Mock(return_value="AU123456789"),
			get_list=Mock(
				return_value=[
					{
						"name": "AUTO-0001",
						"source_purchase_invoice": "PINV-0001",
						"linked_edi_document": "EDI-AUTO-0001",
						"company": "Fabricators",
						"supplier_name": "Atlassian Pty Ltd",
						"document_type": "TD17",
						"document_date": "2026-04-25",
						"document_number": None,
						"naming_series": "AUTO-.YYYY.-",
						"validation_state": "draft",
						"transmission_state": "draft",
						"latest_receipt_state": None,
						"generated_xml": None,
						"last_error": None,
						"modified": "2026-04-25 15:00:00",
					}
				]
			),
			db=SimpleNamespace(exists=Mock(return_value=True)),
		)

		with (
			patch.object(autofattura, "frappe", new=frappe_stub),
			patch.object(
				autofattura,
				"get_purchase_invoice_for_autofattura",
				return_value=SimpleNamespace(
					name="PINV-0001",
					company="Fabricators",
					supplier="SUP-0001",
					supplier_name="Atlassian Pty Ltd",
					posting_date="2026-04-20",
					bill_no="INV-001",
					bill_date="2026-04-19",
					currency="USD",
					credit_to="Creditors - USD - fab - fab",
					net_total=36.2,
					base_net_total=30.91,
					grand_total=36.2,
					base_grand_total=30.91,
					total_taxes_and_charges=0,
					base_total_taxes_and_charges=0,
					items=[
						SimpleNamespace(
							idx=1,
							item_name="Jira, Standard",
							description="Monthly subscription",
							qty=1,
							uom="Month",
							rate=36.2,
							amount=36.2,
							expense_account="6170 - Software subscriptions - fab",
							item_tax_template=None,
						)
					],
					taxes=[],
				),
			),
			patch.object(autofattura, "get_default_company_address", return_value="Office-Billing"),
			patch.object(autofattura, "get_default_address", return_value="Supplier-Billing"),
			patch.object(autofattura, "get_linked_autofattura_transport_document", return_value=None),
		):
			result = autofattura.get_autofattura_dashboard("AUTO-0001")

		self.assertEqual(result["selected_autofattura"]["name"], "AUTO-0001")
		self.assertEqual(
			result["selected_autofattura"]["document_type_label"],
			"TD17 - Purchase of services from abroad",
		)
		self.assertEqual(result["selected_autofattura"]["autofattura_document_date"], "2026-04-25")
		self.assertTrue(result["selected_autofattura"]["can_confirm_review"])
		self.assertTrue(result["selected_autofattura"]["send_supported"])
		self.assertFalse(result["selected_autofattura"]["can_send"])
		self.assertEqual(result["selected_autofattura"]["preview_notices"][0]["level"], "info")
		self.assertEqual(result["selected_autofattura"]["autofattura_tax_summaries"][0]["tax_amount"], "7.96")
		self.assertIn("Confirm autofattura review", result["selected_autofattura"]["send_blockers"][0])
		self.assertEqual(result["selected_autofattura"]["source_purchase_invoice"]["items"][0]["item_name"], "Jira, Standard")

	def test_collect_autofattura_send_blockers_reports_missing_tax_and_supplier_tax_id(self):
		purchase_invoice = SimpleNamespace(
			company="Fabricators",
			supplier="SUP-0001",
			bill_no="INV-001",
			bill_date="2026-04-19",
		)
		document = SimpleNamespace(
			validation_state="valid",
			transmission_state="draft",
			document_date="2026-04-25",
			supplier_invoice_number="INV-001",
			supplier_invoice_date="2026-04-19",
			lines=[
				SimpleNamespace(
					description="Monthly subscription",
					quantity=1,
					unit_of_measure="Month",
					unit_price=36.2,
					total_price=36.2,
					tax_rate=None,
					nature=None,
					reference_law=None,
				)
			],
		)
		frappe_stub = SimpleNamespace(get_cached_value=Mock(return_value=None))

		with (
			patch.object(autofattura, "frappe", new=frappe_stub),
			patch.object(autofattura, "get_default_company_address", return_value="Office-Billing"),
			patch.object(autofattura, "get_default_address", return_value="Supplier-Billing"),
		):
			blockers = autofattura.collect_autofattura_send_blockers(purchase_invoice, document)

		self.assertIn("Set the foreign supplier Tax ID", blockers[0])
		self.assertTrue(any("VAT rate or Natura" in blocker for blocker in blockers))

	def test_build_autofattura_xml_context_includes_issuer_fiscal_regime(self):
		document = SimpleNamespace(
			name="AUTO-0001",
			doctype="Autofattura",
			company="fabricators",
			document_type="TD17",
			document_date="2026-04-25",
			document_number="AUTO-2026-00001",
			supplier_invoice_number="INV-001",
			supplier_invoice_date="2026-04-19",
			currency="USD",
			lines=[
				SimpleNamespace(
					description="Monthly subscription",
					quantity=1,
					unit_of_measure="Month",
					unit_price=36.2,
					total_price=36.2,
					tax_rate=22,
					nature=None,
					reference_law=None,
				)
			],
			generated_xml=None,
		)
		purchase_invoice = SimpleNamespace(
			name="PINV-0001",
			company="fabricators",
			supplier="SUP-0001",
			bill_no="INV-001",
			bill_date="2026-04-19",
			currency="USD",
		)
		company = SimpleNamespace(
			name="fabricators",
			company_name="Fabricators SRL",
			tax_id="IT04266880980",
			fiscal_code="04266880980",
			fiscal_regime="RF01-Ordinario",
		)
		supplier = SimpleNamespace(
			name="SUP-0001",
			supplier_name="Atlassian Pty Ltd",
			tax_id="AU123456789",
		)
		configuration = SimpleNamespace(
			get=lambda fieldname: {
				"recipient_code_fallback": None,
				"sender_vat_id": None,
				"sender_fiscal_code": None,
				"sender_email": "edi@example.com",
			}.get(fieldname)
		)
		frappe_stub = SimpleNamespace(
			get_doc=Mock(side_effect=lambda doctype, name: {"Company": company, "Supplier": supplier}[doctype]),
			db=SimpleNamespace(get_value=Mock(return_value=None)),
			_dict=frappe._dict,
			utils=SimpleNamespace(strip_html=lambda value: value),
		)

		with (
			patch.object(autofattura_xml, "frappe", new=frappe_stub),
			patch.object(autofattura_xml, "get_company_address_data", return_value=frappe._dict(name="Office-Billing", address_line1="Via Roma 1", pincode="00100", city="Roma", state_code="RM", country_code="IT")),
			patch.object(autofattura_xml, "get_supplier_address_data", return_value=frappe._dict(name="Supplier-Billing", address_line1="1 Market St", pincode="2000", city="Sydney", state_code="NSW", country_code="AU")),
			patch.object(autofattura_xml, "get_or_allocate_autofattura_document_number", return_value="AUTO-2026-00001"),
			patch.object(autofattura_xml, "get_or_allocate_progressive_name", return_value=("IT04266880980_00001", "00001")),
		):
			context = autofattura_xml.build_autofattura_xml_context(document, purchase_invoice, configuration)

		self.assertEqual(context["seller"]["country_code"], "AU")
		self.assertEqual(context["seller"]["tax_id"], "123456789")
		self.assertEqual(context["seller"]["fiscal_code"], "04266880980")
		self.assertEqual(context["seller"]["fiscal_regime"], "RF01")

	def test_build_autofattura_xml_context_requires_company_fiscal_regime(self):
		document = SimpleNamespace(
			name="AUTO-0001",
			doctype="Autofattura",
			company="fabricators",
			document_type="TD17",
			document_date="2026-04-25",
			supplier_invoice_number="INV-001",
			supplier_invoice_date="2026-04-19",
			currency="USD",
			lines=[
				SimpleNamespace(
					description="Monthly subscription",
					quantity=1,
					unit_of_measure="Month",
					unit_price=36.2,
					total_price=36.2,
					tax_rate=22,
					nature=None,
					reference_law=None,
				)
			],
			generated_xml=None,
		)
		purchase_invoice = SimpleNamespace(
			name="PINV-0001",
			company="fabricators",
			supplier="SUP-0001",
			bill_no="INV-001",
			bill_date="2026-04-19",
			currency="USD",
		)
		company = SimpleNamespace(
			name="fabricators",
			company_name="Fabricators SRL",
			tax_id="IT04266880980",
			fiscal_code="04266880980",
			fiscal_regime=None,
		)
		supplier = SimpleNamespace(
			name="SUP-0001",
			supplier_name="Atlassian Pty Ltd",
			tax_id="AU123456789",
		)
		configuration = SimpleNamespace(
			get=lambda fieldname: {
				"recipient_code_fallback": None,
				"sender_vat_id": None,
				"sender_fiscal_code": None,
				"sender_email": "edi@example.com",
			}.get(fieldname)
		)
		frappe_stub = SimpleNamespace(
			get_doc=Mock(side_effect=lambda doctype, name: {"Company": company, "Supplier": supplier}[doctype]),
			utils=SimpleNamespace(strip_html=lambda value: value),
		)

		with (
			patch.object(autofattura_xml, "frappe", new=frappe_stub),
			patch.object(autofattura_xml, "get_company_address_data", return_value=frappe._dict(name="Office-Billing", address_line1="Via Roma 1", pincode="00100", city="Roma", state_code="RM", country_code="IT")),
			patch.object(autofattura_xml, "get_supplier_address_data", return_value=frappe._dict(name="Supplier-Billing", address_line1="1 Market St", pincode="2000", city="Sydney", state_code="NSW", country_code="AU")),
			patch.object(autofattura_xml, "get_or_allocate_autofattura_document_number", return_value="AUTO-2026-00001"),
			patch.object(autofattura_xml, "get_or_allocate_progressive_name", return_value=("IT04266880980_00001", "00001")),
		):
			with self.assertRaises(frappe.ValidationError):
				autofattura_xml.build_autofattura_xml_context(document, purchase_invoice, configuration)

	def test_send_autofattura_to_sdi_queues_worker_and_updates_status(self):
		autofattura_doc = SimpleNamespace(
			name="AUTO-0001",
			source_purchase_invoice="PINV-0001",
			company="fabricators",
			validation_state="valid",
			transmission_state="draft",
			latest_receipt_state=None,
			last_error=None,
			document_number=None,
			generated_xml=None,
			save=Mock(),
		)
		purchase_invoice = SimpleNamespace(name="PINV-0001", supplier="SUP-0001", add_comment=Mock())
		configuration = SimpleNamespace(name="fabricators", default_provider="OpenAPI SDI Proxy")
		provider = SimpleNamespace(name="OpenAPI SDI Proxy", channel="sdi_proxy")
		transport_doc = FakeEDIDocument(
			name="EDI-AUTO-0001",
			document_kind="autofattura",
			source_doctype="Autofattura",
			source_name="AUTO-0001",
			company="fabricators",
			validation_state="draft",
			transmission_state="draft",
			latest_receipt_state=None,
			external_submission_id=None,
			last_error=None,
			transmission_attempts=[],
		)
		adapter = SimpleNamespace(submit_outbound_invoice=Mock(return_value={"uuid": "uuid-123"}))
		adapter.ensure_outbound_submission_ready = Mock()
		rendered_file = SimpleNamespace(
			file_url="/private/files/autofattura.xml",
			file_name="IT04266880980_00009.xml",
			document_number="AUTO-2026-00001",
		)
		frappe_stub = SimpleNamespace(has_permission=Mock())

		with (
			patch.object(autofattura, "_", side_effect=lambda text: text),
			patch.object(autofattura, "frappe", new=frappe_stub),
			patch("fab_italy_edi.api.frappe.enqueue") as enqueue,
			patch.object(autofattura, "get_autofattura", return_value=autofattura_doc),
			patch.object(autofattura, "get_or_create_autofattura_transport_document", return_value=transport_doc),
			patch.object(autofattura, "get_purchase_invoice_for_autofattura", return_value=purchase_invoice),
			patch.object(autofattura, "collect_autofattura_send_blockers", return_value=[]),
			patch.object(autofattura, "get_enabled_edi_configuration", return_value=configuration),
			patch.object(autofattura, "get_enabled_provider", return_value=provider),
			patch.object(autofattura, "get_provider_adapter", return_value=adapter),
			patch.object(autofattura, "sync_transport_document_from_autofattura"),
			patch.object(
				autofattura,
				"sync_autofattura_from_transport_document",
				side_effect=lambda doc, transport, save=False: doc,
			),
			patch.object(autofattura, "render_and_attach_autofattura_xml", return_value=rendered_file),
		):
			result = autofattura.send_autofattura_to_sdi("AUTO-0001")

		self.assertEqual(
			result,
			{
				"autofattura": "AUTO-0001",
				"edi_document": "EDI-AUTO-0001",
				"external_submission_id": None,
				"transmission_state": "queued",
				"latest_receipt_state": "queued",
				"autofattura_document_number": "AUTO-2026-00001",
			},
		)
		self.assertEqual(transport_doc.generated_xml, "/private/files/autofattura.xml")
		self.assertEqual(transport_doc.transmission_state, "queued")
		self.assertEqual(transport_doc.latest_receipt_state, "queued")
		self.assertEqual(len(transport_doc.transmission_attempts), 0)
		self.assertEqual(transport_doc.save_calls, 1)
		adapter.ensure_outbound_submission_ready.assert_called_once_with(
			configuration=configuration,
			provider=provider,
		)
		adapter.submit_outbound_invoice.assert_not_called()
		enqueue.assert_called_once_with(
			"fab_italy_edi.api.run_outbound_send_job",
			queue="long",
			job_name="fab_italy_edi:outbound-send:EDI-AUTO-0001",
			enqueue_after_commit=True,
			document_name="EDI-AUTO-0001",
		)
		purchase_invoice.add_comment.assert_not_called()
