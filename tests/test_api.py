from __future__ import annotations

import json
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

from frappe.exceptions import ValidationError

from fab_italy_edi import api


class FakeEDIDocument:
	def __init__(self, **kwargs):
		self.name = kwargs.pop("name", "EDI-DOC-0001")
		self.transmission_attempts = []
		self.__dict__.update(kwargs)
		self.insert_calls = 0
		self.save_calls = 0

	def insert(self, ignore_permissions=False):
		self.insert_calls += 1
		return self

	def save(self, ignore_permissions=False):
		self.save_calls += 1
		return self

	def append(self, fieldname, value):
		getattr(self, fieldname).append(SimpleNamespace(**value))


class TestSendSalesInvoiceToSDI(unittest.TestCase):
	def test_get_or_create_outbound_document_uses_credit_note_kind_for_returns(self):
		invoice = SimpleNamespace(
			doctype="Sales Invoice",
			name="ACC-SINV-RET-2026-00001",
			company="fabricators",
			customer="Pippo SRL",
			customer_name="Pippo SRL",
			is_return=1,
		)
		provider = SimpleNamespace(name="OpenAPI SDI Proxy", channel="sdi_proxy")
		document = FakeEDIDocument(document_kind="credit_note")

		with (
			patch.object(api.frappe, "db", new=SimpleNamespace(get_value=Mock(return_value=None))),
			patch.object(api.frappe, "get_doc", return_value=document),
		):
			result = api.get_or_create_outbound_document(invoice, provider)

		self.assertEqual(result, document)
		self.assertEqual(document.document_kind, "credit_note")
		self.assertEqual(document.insert_calls, 1)

	def test_send_sales_invoice_to_sdi_queues_worker_and_updates_status(self):
		invoice = SimpleNamespace(
			doctype="Sales Invoice",
			name="ACC-SINV-2026-00001",
			docstatus=1,
			company="fabricators",
			customer="Pippo SRL",
			customer_name="Pippo SRL",
			is_return=0,
		)
		configuration = SimpleNamespace(name="fabricators", enabled=1, default_provider="OpenAPI SDI Proxy")
		provider = SimpleNamespace(name="OpenAPI SDI Proxy", enabled=1, channel="sdi_proxy", adapter_key="openapi")
		document = FakeEDIDocument(
			doctype="EDI Document",
			source_doctype="Sales Invoice",
			source_name=invoice.name,
			company=invoice.company,
			document_kind="invoice",
			party_name=invoice.customer_name,
			channel=provider.channel,
			provider=provider.name,
			validation_state="draft",
			transmission_state="draft",
			latest_receipt_state=None,
			external_submission_id=None,
			last_error=None,
		)
		generated_file = SimpleNamespace(file_url="/private/files/test.xml", file_name="IT04266880980_00003.xml")
		adapter = SimpleNamespace(submit_outbound_invoice=Mock(return_value={"uuid": "uuid-123"}))
		adapter.ensure_outbound_submission_ready = Mock()

		def get_doc_side_effect(*args, **kwargs):
			if args == ("Sales Invoice", invoice.name):
				return invoice
			if args == ("EDI Configuration", invoice.company):
				return configuration
			if args == ("EDI Provider", provider.name):
				return provider
			if len(args) == 1 and isinstance(args[0], dict) and args[0].get("doctype") == "EDI Document":
				return document
			raise AssertionError(f"Unexpected get_doc call: {args!r}")

		with (
			patch.object(api, "_", side_effect=lambda text: text),
			patch.object(api.frappe, "get_doc", side_effect=get_doc_side_effect),
			patch.object(api.frappe, "has_permission"),
			patch.object(api.frappe, "db", new=SimpleNamespace(get_value=Mock(return_value=None))),
			patch.object(api.frappe, "enqueue") as enqueue,
			patch.object(api, "get_provider_adapter", return_value=adapter),
			patch.object(api, "generate_sales_invoice_xml", return_value=generated_file),
			patch.object(api, "sync_sales_invoice_tracking") as sync_tracking,
		):
			result = api.send_sales_invoice_to_sdi(invoice.name)

		self.assertEqual(
			result,
			{
				"edi_document": "EDI-DOC-0001",
				"external_submission_id": None,
				"transmission_state": "queued",
				"latest_receipt_state": "queued",
			},
		)
		self.assertEqual(document.generated_xml, generated_file.file_url)
		self.assertEqual(document.validation_state, "valid")
		self.assertIsNone(document.external_submission_id)
		self.assertEqual(document.transmission_state, "queued")
		self.assertEqual(document.latest_receipt_state, "queued")
		self.assertIsNone(document.last_error)
		self.assertEqual(len(document.transmission_attempts), 0)
		self.assertEqual(document.insert_calls, 1)
		self.assertEqual(document.save_calls, 1)
		adapter.ensure_outbound_submission_ready.assert_called_once_with(
			configuration=configuration,
			provider=provider,
		)
		adapter.submit_outbound_invoice.assert_not_called()
		enqueue.assert_called_once_with(
			"fab_italy_edi.api.run_outbound_send_job",
			queue=api.OUTBOUND_SEND_QUEUE,
			job_name="fab_italy_edi:outbound-send:EDI-DOC-0001",
			enqueue_after_commit=True,
			document_name="EDI-DOC-0001",
		)
		sync_tracking.assert_called_once_with(document, activity_message=None)

	def test_send_sales_invoice_to_sdi_blocks_duplicate_active_submission(self):
		invoice = SimpleNamespace(
			doctype="Sales Invoice",
			name="ACC-SINV-2026-00001",
			docstatus=1,
			company="fabricators",
			customer="Pippo SRL",
			customer_name="Pippo SRL",
			is_return=0,
		)
		configuration = SimpleNamespace(name="fabricators", enabled=1, default_provider="OpenAPI SDI Proxy")
		provider = SimpleNamespace(name="OpenAPI SDI Proxy", enabled=1, channel="sdi_proxy", adapter_key="openapi")
		document = FakeEDIDocument(
			name="EDI-DOC-0001",
			source_doctype="Sales Invoice",
			source_name=invoice.name,
			company=invoice.company,
			document_kind="invoice",
			party_name=invoice.customer_name,
			channel=provider.channel,
			provider=provider.name,
			validation_state="valid",
			transmission_state="queued",
			latest_receipt_state="queued",
			external_submission_id="uuid-123",
			last_error=None,
		)

		def get_doc_side_effect(*args, **kwargs):
			if args == ("Sales Invoice", invoice.name):
				return invoice
			if args == ("EDI Configuration", invoice.company):
				return configuration
			if args == ("EDI Provider", provider.name):
				return provider
			if args == ("EDI Document", "EDI-DOC-0001"):
				return document
			raise AssertionError(f"Unexpected get_doc call: {args!r}")

		with (
			patch.object(api, "_", side_effect=lambda text: text),
			patch.object(api.frappe, "get_doc", side_effect=get_doc_side_effect),
			patch.object(api.frappe, "has_permission"),
			patch.object(api.frappe, "db", new=SimpleNamespace(get_value=Mock(return_value="EDI-DOC-0001"))),
			patch.object(api, "get_provider_adapter", return_value=SimpleNamespace()),
		):
			with self.assertRaises(ValidationError):
				api.send_sales_invoice_to_sdi(invoice.name)

	def test_run_outbound_send_job_persists_successful_attempt(self):
		invoice = SimpleNamespace(
			doctype="Sales Invoice",
			name="ACC-SINV-2026-00001",
			docstatus=1,
			company="fabricators",
			customer="Pippo SRL",
			customer_name="Pippo SRL",
			is_return=0,
		)
		configuration = SimpleNamespace(name="fabricators", enabled=1, default_provider="OpenAPI SDI Proxy")
		provider = SimpleNamespace(name="OpenAPI SDI Proxy", enabled=1, channel="sdi_proxy", adapter_key="openapi")
		document = FakeEDIDocument(
			doctype="EDI Document",
			source_doctype="Sales Invoice",
			source_name=invoice.name,
			company=invoice.company,
			document_kind="invoice",
			party_name=invoice.customer_name,
			channel=provider.channel,
			provider=provider.name,
			generated_xml="/private/files/test.xml",
			validation_state="valid",
			transmission_state="queued",
			latest_receipt_state="queued",
			external_submission_id=None,
			last_error=None,
		)
		adapter = SimpleNamespace(submit_outbound_invoice=Mock(return_value={"uuid": "uuid-123"}))
		adapter.ensure_outbound_submission_ready = Mock()

		def get_doc_side_effect(*args, **kwargs):
			if args == ("EDI Document", document.name):
				return document
			if args == ("EDI Configuration", invoice.company):
				return configuration
			if args == ("EDI Provider", provider.name):
				return provider
			raise AssertionError(f"Unexpected get_doc call: {args!r}")

		@contextmanager
		def fake_automation_user_context():
			yield "fab-edi-bot@example.invalid"

		with (
			patch.object(api, "_", side_effect=lambda text: text),
			patch.object(api.frappe, "get_doc", side_effect=get_doc_side_effect),
			patch.object(api, "automation_user_context", fake_automation_user_context),
			patch.object(api, "get_provider_adapter", return_value=adapter),
			patch.object(api, "read_site_file", return_value="<xml />"),
			patch.object(
				api,
				"persist_payload_artifact",
				side_effect=["/private/files/request.xml", "/private/files/response.json"],
			),
			patch.object(api, "sync_outbound_source_tracking") as sync_tracking,
		):
			result = api.run_outbound_send_job(document.name)

		self.assertEqual(
			result,
			{
				"edi_document": "EDI-DOC-0001",
				"external_submission_id": "uuid-123",
				"transmission_state": "queued",
				"latest_receipt_state": "queued",
			},
		)
		self.assertEqual(document.transmission_state, "queued")
		self.assertEqual(document.latest_receipt_state, "queued")
		self.assertIsNone(document.last_error)
		self.assertEqual(len(document.transmission_attempts), 1)
		self.assertEqual(document.transmission_attempts[0].transport_status, "success")
		self.assertEqual(document.transmission_attempts[0].external_reference_id, "uuid-123")
		self.assertEqual(document.save_calls, 2)
		adapter.ensure_outbound_submission_ready.assert_called_once_with(
			configuration=configuration,
			provider=provider,
		)
		adapter.submit_outbound_invoice.assert_called_once_with(
			provider=provider,
			configuration=configuration,
			document=document,
			xml_content="<xml />",
		)
		self.assertEqual(
			sync_tracking.call_args_list,
			[
				call(document),
				call(document, activity_message="FAB EDI queued invoice to SDI proxy as uuid-123."),
			],
		)

	def test_run_outbound_send_job_persists_failed_attempt(self):
		invoice = SimpleNamespace(
			doctype="Sales Invoice",
			name="ACC-SINV-2026-00001",
			docstatus=1,
			company="fabricators",
			customer="Pippo SRL",
			customer_name="Pippo SRL",
			is_return=0,
		)
		configuration = SimpleNamespace(name="fabricators", enabled=1, default_provider="OpenAPI SDI Proxy")
		provider = SimpleNamespace(name="OpenAPI SDI Proxy", enabled=1, channel="sdi_proxy", adapter_key="openapi")
		document = FakeEDIDocument(
			doctype="EDI Document",
			source_doctype="Sales Invoice",
			source_name=invoice.name,
			company=invoice.company,
			document_kind="invoice",
			party_name=invoice.customer_name,
			channel=provider.channel,
			provider=provider.name,
			generated_xml="/private/files/test.xml",
			validation_state="valid",
			transmission_state="queued",
			latest_receipt_state="queued",
			external_submission_id=None,
			last_error=None,
		)
		adapter = SimpleNamespace(
			submit_outbound_invoice=Mock(side_effect=ValidationError("OpenAPI request failed with status 403: 389"))
		)
		adapter.ensure_outbound_submission_ready = Mock()

		def get_doc_side_effect(*args, **kwargs):
			if args == ("EDI Document", document.name):
				return document
			if args == ("EDI Configuration", invoice.company):
				return configuration
			if args == ("EDI Provider", provider.name):
				return provider
			raise AssertionError(f"Unexpected get_doc call: {args!r}")

		@contextmanager
		def fake_automation_user_context():
			yield "fab-edi-bot@example.invalid"

		with (
			patch.object(api, "_", side_effect=lambda text: text),
			patch.object(api.frappe, "get_doc", side_effect=get_doc_side_effect),
			patch.object(api, "automation_user_context", fake_automation_user_context),
			patch.object(api, "get_provider_adapter", return_value=adapter),
			patch.object(api, "read_site_file", return_value="<xml />"),
			patch.object(api, "persist_payload_artifact", return_value="/private/files/request.xml"),
			patch.object(api, "sync_outbound_source_tracking") as sync_tracking,
		):
			result = api.run_outbound_send_job(document.name)

		self.assertEqual(
			result,
			{
				"edi_document": "EDI-DOC-0001",
				"external_submission_id": None,
				"transmission_state": "failed",
				"latest_receipt_state": "failed",
				"last_error": "OpenAPI request failed with status 403: 389",
			},
		)
		self.assertEqual(document.transmission_state, "failed")
		self.assertEqual(document.latest_receipt_state, "failed")
		self.assertEqual(document.last_error, "OpenAPI request failed with status 403: 389")
		self.assertEqual(len(document.transmission_attempts), 1)
		self.assertEqual(document.transmission_attempts[0].transport_status, "failed")
		self.assertEqual(document.save_calls, 2)
		adapter.ensure_outbound_submission_ready.assert_called_once_with(
			configuration=configuration,
			provider=provider,
		)
		self.assertEqual(
			sync_tracking.call_args_list,
			[
				call(document),
				call(
					document,
					activity_message="FAB EDI send to SDI failed: OpenAPI request failed with status 403: 389",
				),
			],
		)


class TestOpenAPICallback(unittest.TestCase):
	def test_receive_openapi_callback_uses_automation_user_for_notifications(self):
		document = SimpleNamespace(name="EDI-DOC-0001")
		adapter = SimpleNamespace(
			adapter_key="openapi",
			extract_callback_event=Mock(return_value="customer-notification"),
			extract_callback_notification=Mock(
				return_value={"invoice_uuid": "uuid-123", "uuid": "ntf-123"}
			),
			get_notification_external_submission_id=Mock(return_value="uuid-123"),
		)
		request = SimpleNamespace(
			method="POST",
			get_data=lambda as_text=True: json.dumps(
				{
					"event": "customer-notification",
					"data": {"notification": {"invoice_uuid": "uuid-123", "uuid": "ntf-123"}},
				}
			),
		)
		local = SimpleNamespace(response={})
		markers = []

		@contextmanager
		def fake_automation_user_context():
			markers.append("enter")
			yield "fab-edi-bot@example.invalid"
			markers.append("exit")

		with (
			patch.object(api.frappe, "request", new=request),
			patch.object(api.frappe, "local", new=local),
			patch.object(api, "get_adapter_class", return_value=lambda: adapter),
			patch.object(api, "get_document_by_external_submission_id", return_value=document),
			patch.object(api, "automation_user_context", fake_automation_user_context),
			patch.object(api, "sync_notification") as sync_notification,
		):
			result = api.receive_openapi_callback()

		self.assertEqual(result, {"ok": True})
		self.assertEqual(local.response["http_status_code"], 202)
		self.assertEqual(markers, ["enter", "exit"])
		sync_notification.assert_called_once_with(
			document,
			adapter,
			{"invoice_uuid": "uuid-123", "uuid": "ntf-123"},
		)


class TestOutboundSourceTracking(unittest.TestCase):
	def test_sync_outbound_source_tracking_updates_autofattura(self):
		document = SimpleNamespace(source_doctype="Autofattura", source_name="AUTO-0001")
		autofattura_doc = SimpleNamespace(source_purchase_invoice="PINV-0001")
		purchase_invoice = SimpleNamespace(add_comment=Mock())

		with (
			patch("fab_italy_edi.autofattura.get_autofattura", return_value=autofattura_doc),
			patch("fab_italy_edi.autofattura.sync_autofattura_from_transport_document") as sync_autofattura,
			patch("fab_italy_edi.autofattura.get_purchase_invoice_for_autofattura", return_value=purchase_invoice),
		):
			api.sync_outbound_source_tracking(
				document,
				activity_message="Queued autofattura AUTO-0001 to SDI as uuid-123.",
			)

		sync_autofattura.assert_called_once_with(autofattura_doc, document, save=True)
		purchase_invoice.add_comment.assert_called_once_with(
			"Info",
			"Queued autofattura AUTO-0001 to SDI as uuid-123.",
		)

	def test_receive_openapi_callback_imports_supplier_invoice(self):
		configuration = SimpleNamespace(name="fabricators", default_provider="OpenAPI SDI Proxy")
		provider = SimpleNamespace(name="OpenAPI SDI Proxy", adapter_key="openapi", enabled=1)
		normalized_invoice = {"uuid": "sup-uuid"}
		adapter = SimpleNamespace(
			adapter_key="openapi",
			extract_callback_event=Mock(return_value="supplier-invoice"),
			extract_callback_supplier_invoice=Mock(return_value=normalized_invoice),
		)
		request = SimpleNamespace(
			method="POST",
			get_data=lambda as_text=True: json.dumps(
				{
					"event": "supplier-invoice",
					"data": {"invoice": {"uuid": "sup-uuid", "filename": "IT123.xml", "payload": {}}},
				}
			),
		)
		local = SimpleNamespace(response={})
		markers = []

		@contextmanager
		def fake_automation_user_context():
			markers.append("enter")
			yield "fab-edi-bot@example.invalid"
			markers.append("exit")

		with (
			patch.object(api.frappe, "request", new=request),
			patch.object(api.frappe, "local", new=local),
			patch.object(api, "get_adapter_class", return_value=lambda: adapter),
			patch.object(api, "find_incoming_edi_configuration_for_invoice", return_value=configuration),
			patch.object(api, "get_enabled_provider", return_value=provider),
			patch.object(api, "automation_user_context", fake_automation_user_context),
			patch.object(api, "upsert_incoming_invoice_document") as upsert_incoming_invoice_document,
		):
			result = api.receive_openapi_callback()

		self.assertEqual(result, {"ok": True})
		self.assertEqual(local.response["http_status_code"], 202)
		self.assertEqual(markers, ["enter", "exit"])
		upsert_incoming_invoice_document.assert_called_once_with(
			configuration,
			provider,
			adapter,
			normalized_invoice,
		)

	def test_receive_openapi_callback_persists_legal_storage_receipt(self):
		document = SimpleNamespace(
			name="EDI-DOC-0001",
			latest_receipt_state="accepted",
			save=Mock(),
		)
		request = SimpleNamespace(
			method="POST",
			get_data=lambda as_text=True: json.dumps(
				{
					"event": "legal-storage-receipt",
					"data": {
						"uuid": "lsr-123",
						"object_id": "uuid-123",
						"status": "stored",
						"preserved_object_id": "pres-123",
					},
				}
			),
		)
		local = SimpleNamespace(response={})
		adapter = SimpleNamespace(
			adapter_key="openapi",
			extract_callback_event=Mock(return_value="legal-storage-receipt"),
		)

		@contextmanager
		def fake_automation_user_context():
			yield "fab-edi-bot@example.invalid"

		with (
			patch.object(api.frappe, "request", new=request),
			patch.object(api.frappe, "local", new=local),
			patch.object(api, "get_adapter_class", return_value=lambda: adapter),
			patch.object(api, "get_document_by_external_submission_id", return_value=document),
			patch.object(api, "automation_user_context", fake_automation_user_context),
			patch.object(api, "persist_payload_artifact", return_value="/private/files/legal-storage.json"),
			patch.object(api, "append_edi_receipt", return_value=True) as append_edi_receipt,
			patch.object(
				api,
				"reconcile_edi_document_states",
				return_value=(False, "FAB EDI received OpenAPI legal-storage-receipt: status Accepted"),
			),
			patch.object(api, "sync_outbound_source_tracking") as sync_outbound_source_tracking,
		):
			result = api.receive_openapi_callback()

		self.assertEqual(result, {"ok": True})
		append_edi_receipt.assert_called_once()
		sync_outbound_source_tracking.assert_called_once_with(
			document,
			activity_message="FAB EDI received OpenAPI legal-storage-receipt: status Accepted",
		)
		document.save.assert_called_once_with(ignore_permissions=True)

	def test_receive_openapi_callback_persists_legal_storage_missing_vat(self):
		document = SimpleNamespace(
			name="EDI-DOC-0001",
			latest_receipt_state="delivered",
			save=Mock(),
		)
		request = SimpleNamespace(
			method="POST",
			get_data=lambda as_text=True: json.dumps(
				{
					"event": "legal-storage-missing-vat",
					"data": {
						"uuid": "lsm-123",
						"object_id": "uuid-123",
						"message": "Missing VAT metadata",
					},
				}
			),
		)
		local = SimpleNamespace(response={})
		adapter = SimpleNamespace(
			adapter_key="openapi",
			extract_callback_event=Mock(return_value="legal-storage-missing-vat"),
		)

		@contextmanager
		def fake_automation_user_context():
			yield "fab-edi-bot@example.invalid"

		with (
			patch.object(api.frappe, "request", new=request),
			patch.object(api.frappe, "local", new=local),
			patch.object(api, "get_adapter_class", return_value=lambda: adapter),
			patch.object(api, "get_document_by_external_submission_id", return_value=document),
			patch.object(api, "automation_user_context", fake_automation_user_context),
			patch.object(api, "persist_payload_artifact", return_value="/private/files/legal-storage-missing-vat.json"),
			patch.object(api, "append_edi_receipt", return_value=True) as append_edi_receipt,
			patch.object(api, "reconcile_edi_document_states", return_value=(False, None)),
		):
			result = api.receive_openapi_callback()

		self.assertEqual(result, {"ok": True})
		append_edi_receipt.assert_called_once()
		document.save.assert_called_once_with(ignore_permissions=True)


class TestOpenAPIInboundHelpers(unittest.TestCase):
	def test_find_incoming_edi_configuration_matches_recipient_identity(self):
		adapter = SimpleNamespace(
			normalize_incoming_invoice=Mock(
				return_value={"recipient_identifiers": ["04266880980"]}
			)
		)
		rows = [
			{
				"name": "fabricators",
				"company": "fabricators",
				"default_provider": "OpenAPI SDI Proxy",
				"sender_vat_id": "IT04266880980",
				"sender_fiscal_code": None,
			}
		]
		configuration = SimpleNamespace(name="fabricators", company="fabricators")

		with (
			patch.object(api.frappe, "get_all", return_value=rows),
			patch.object(api.frappe, "get_cached_value", return_value=None),
			patch.object(api.frappe, "get_doc", return_value=configuration),
		):
			result = api.find_incoming_edi_configuration_for_invoice(adapter, {"uuid": "sup-uuid"})

		self.assertEqual(result, configuration)
