from __future__ import annotations

import unittest
from email.message import EmailMessage
from types import SimpleNamespace
from unittest.mock import Mock, patch

from frappe.exceptions import ValidationError

from fab_italy_edi.channels.sdi_pec.base import SDIPECChannel
from fab_italy_edi.fab_italy_e_invoicing.doctype.edi_configuration.edi_configuration import (
	validate_pec_mailbox_settings,
)


class StubConfiguration(SimpleNamespace):
	def get(self, fieldname):
		return getattr(self, fieldname, None)

	def get_password(self, fieldname, raise_exception=False):
		return getattr(self, fieldname, None)


class StubEDIDocument(SimpleNamespace):
	def append(self, fieldname, value):
		getattr(self, fieldname).append(SimpleNamespace(**value))


class TestPECMailboxValidation(unittest.TestCase):
	def test_validate_pec_mailbox_settings_requires_mailbox_fields_for_pec_provider(self):
		document = StubConfiguration(
			default_provider="PEC Mailbox",
			pec_username="",
			pec_password="",
			pec_imap_host="",
			pec_imap_port="",
			pec_smtp_host="",
			pec_smtp_port="",
		)
		frappe_stub = SimpleNamespace(
			db=SimpleNamespace(
				get_value=Mock(return_value={"channel": "sdi_pec", "adapter_key": "sdi_pec"})
			),
			throw=Mock(side_effect=ValidationError),
		)

		with (
			patch(
				"fab_italy_edi.fab_italy_e_invoicing.doctype.edi_configuration.edi_configuration.frappe",
				new=frappe_stub,
			),
			self.assertRaisesRegex(ValidationError, "PEC Username"),
		):
			validate_pec_mailbox_settings(document)


class TestPECAdapter(unittest.TestCase):
	def test_submit_outbound_invoice_sends_message_via_smtp(self):
		adapter = SDIPECChannel()
		configuration = StubConfiguration(
			pec_username="billing@pec.example.com",
			pec_password="secret",
			pec_imap_host="imap.pec.example.com",
			pec_imap_port=993,
			pec_use_ssl_for_imap=1,
			pec_smtp_host="smtp.pec.example.com",
			pec_smtp_port=465,
			pec_use_ssl_for_smtp=1,
			sender_pec_address="billing@pec.example.com",
			sender_email="billing@example.com",
		)
		provider = SimpleNamespace(
			additional_settings={
				"recipient_address": "sdi@pec.fatturapa.it",
			}
		)
		document = SimpleNamespace(
			name="EDI-DOC-0001",
			source_name="ACC-SINV-2026-00001",
			canonical_identifier="IT04266880980_00001.xml",
		)
		smtp_connection = Mock()
		smtp_connection.__enter__ = Mock(return_value=smtp_connection)
		smtp_connection.__exit__ = Mock(return_value=False)

		with patch(
			"fab_italy_edi.channels.sdi_pec.base.smtplib.SMTP_SSL",
			return_value=smtp_connection,
		):
			result = adapter.submit_outbound_invoice(
				provider,
				"<xml />",
				configuration=configuration,
				document=document,
			)

		smtp_connection.login.assert_called_once_with("billing@pec.example.com", "secret")
		smtp_connection.send_message.assert_called_once()
		message = smtp_connection.send_message.call_args.args[0]
		self.assertEqual(message["To"], "sdi@pec.fatturapa.it")
		self.assertEqual(message["From"], "billing@pec.example.com")
		self.assertEqual(message["Subject"], "FAB EDI PEC submission ACC-SINV-2026-00001")
		self.assertEqual(result["recipient"], "sdi@pec.fatturapa.it")
		self.assertEqual(result["subject"], "FAB EDI PEC submission ACC-SINV-2026-00001")
		self.assertNotIn("<", result["uuid"])
		self.assertNotIn(">", result["uuid"])

	def test_poll_outbound_statuses_reconciles_matching_receipt(self):
		adapter = SDIPECChannel()
		configuration = StubConfiguration(
			name="fabricators",
			company="Fabricators",
			pec_username="billing@pec.example.com",
			pec_password="secret",
			pec_imap_host="imap.pec.example.com",
			pec_imap_port=993,
			pec_use_ssl_for_imap=1,
			pec_smtp_host="smtp.pec.example.com",
			pec_smtp_port=465,
			pec_use_ssl_for_smtp=1,
			sender_pec_address="billing@pec.example.com",
			sender_email="billing@example.com",
		)
		provider = SimpleNamespace(name="PEC Mailbox", additional_settings={})
		document = StubEDIDocument(
			doctype="EDI Document",
			name="EDI-DOC-0001",
			source_doctype="Sales Invoice",
			source_name="ACC-SINV-2026-00001",
			company="Fabricators",
			provider="PEC Mailbox",
			receipts=[],
			transmission_state="queued",
			latest_receipt_state="queued",
			external_submission_id="smtp-original-123",
			last_error=None,
			save=Mock(),
		)
		message = EmailMessage()
		message["Subject"] = "Notifica di scarto IT04266880980_00001.xml"
		message["From"] = "postmaster@sdi.example"
		message["Message-ID"] = "<receipt-123@example.com>"
		message["In-Reply-To"] = "<smtp-original-123>"
		message.set_content("Notifica di scarto")
		raw_message = message.as_bytes()

		class FakeIMAP:
			def select(self, folder):
				return ("OK", [b"1"])

			def search(self, charset, *criteria):
				return ("OK", [b"1"])

			def fetch(self, mailbox_message_id, query):
				return ("OK", [(b"1 (RFC822 {0})", raw_message)])

			def logout(self):
				return ("BYE", [b"logout"])

		frappe_stub = SimpleNamespace(
			db=SimpleNamespace(get_value=Mock(side_effect=["EDI-DOC-0001"])),
			get_doc=Mock(return_value=document),
		)

		with (
			patch.object(adapter, "open_imap_connection", return_value=FakeIMAP()),
			patch(
				"fab_italy_edi.channels.sdi_pec.base.frappe",
				new=frappe_stub,
			),
			patch(
				"fab_italy_edi.channels.sdi_pec.base.edi_lifecycle.persist_payload_artifact",
				return_value="/private/files/pec-receipt.txt",
			),
			patch("fab_italy_edi.channels.sdi_pec.base.sync_sales_invoice_tracking") as sync_tracking,
		):
			result = adapter.poll_outbound_statuses(configuration, provider)

		self.assertEqual(
			result,
			{
				"processed": 1,
				"updated": 1,
				"matched": 1,
				"unmatched": 0,
			},
		)
		self.assertEqual(document.transmission_state, "rejected")
		self.assertEqual(document.latest_receipt_state, "rejected")
		self.assertEqual(document.last_error.splitlines()[0], "receipt_type: NS")
		self.assertEqual(len(document.receipts), 1)
		self.assertEqual(document.receipts[0].receipt_type, "NS")
		self.assertEqual(document.receipts[0].normalized_state, "rejected")
		document.save.assert_called_once_with(ignore_permissions=True)
		sync_tracking.assert_called_once()
