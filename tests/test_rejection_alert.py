import unittest
from unittest.mock import Mock, patch

import frappe

from fab_italy_edi import rejection_alert

NS_ERROR = (
	"type: NS\n"
	"identificativo_sdi: 18446768698\n"
	"nome_file: IT04266880980_00001.xml\n"
	"message_id: 44233396364\n"
	"error: 00200 File non conforme al formato : The value 'N4892' of element 'CAP' is not valid.\n"
	"riga: 1 - colonna: 1129"
)


class FakeEDIDocument(frappe._dict):
	def __init__(self, previous=None, **kwargs):
		super().__init__(name="EDI-0001", **kwargs)
		self._previous = previous

	def get_doc_before_save(self):
		return self._previous


def edi_document(transmission_state, receipt_state=None, previous=None):
	return FakeEDIDocument(
		previous=previous,
		company="FABRICATORS S.R.L.",
		source_doctype="Autofattura",
		source_name="AUTOFATT-0001",
		autofattura_document_number="AUTOFATT/2026/00001",
		party_name="Internet Domain Service BS Corp.",
		transmission_state=transmission_state,
		latest_receipt_state=receipt_state,
		last_error=NS_ERROR,
	)


class TestAlertOnRejection(unittest.TestCase):
	def alerts(self, document):
		with patch.object(rejection_alert, "send_rejection_alert") as send:
			rejection_alert.alert_on_rejection(document)
		return send.call_count

	def test_the_save_that_rejects_the_document_alerts(self):
		self.assertEqual(self.alerts(edi_document("rejected", "rejected", previous=edi_document("sent", "sent"))), 1)

	def test_a_later_save_of_a_rejected_document_stays_quiet(self):
		previous = edi_document("rejected", "rejected")
		self.assertEqual(self.alerts(edi_document("rejected", "rejected", previous=previous)), 0)

	def test_a_rejection_read_off_the_receipt_alone_alerts(self):
		self.assertEqual(self.alerts(edi_document("sent", "rejected", previous=edi_document("sent", "sent"))), 1)

	def test_a_document_inserted_as_rejected_alerts(self):
		self.assertEqual(self.alerts(edi_document("rejected")), 1)

	def test_a_retry_rejected_again_alerts_again(self):
		# the resend puts both states back to queued
		self.assertEqual(self.alerts(edi_document("rejected", "rejected", previous=edi_document("queued", "queued"))), 1)

	def test_a_failed_send_alerts_once(self):
		self.assertEqual(self.alerts(edi_document("failed", previous=edi_document("sending"))), 1)
		self.assertEqual(self.alerts(edi_document("failed", "failed", previous=edi_document("failed", "failed"))), 0)

	def test_a_failed_send_has_its_own_subject(self):
		with patch.object(rejection_alert, "get_url_to_form", return_value="https://x/app/sales-invoice/S"):
			document = edi_document("failed", previous=edi_document("sending"))
			document.update(source_doctype="Sales Invoice", source_name="S", last_error="proxy down")
			subject, _message = rejection_alert.build_rejection_alert(document)
		self.assertTrue(subject.startswith("[SdI send failed]"))

	def test_a_mail_failure_is_logged_and_never_raised(self):
		with (
			patch.object(rejection_alert, "send_rejection_alert", side_effect=RuntimeError("smtp down")),
			patch.object(rejection_alert.frappe, "log_error") as log_error,
		):
			rejection_alert.alert_on_rejection(edi_document("rejected", previous=edi_document("sent")))
		log_error.assert_called_once()


class TestRejectionAlertMessage(unittest.TestCase):
	def test_the_reason_is_the_error_line(self):
		self.assertEqual(
			rejection_alert.get_sdi_error_reason(NS_ERROR),
			"00200 File non conforme al formato : The value 'N4892' of element 'CAP' is not valid.",
		)

	def test_several_error_lines_are_joined(self):
		text = "type: NS\nerror: 00200 first\nerror: 00305 second"
		self.assertEqual(rejection_alert.get_sdi_error_reason(text), "00200 first; 00305 second")

	def test_without_an_error_line_the_reason_is_the_first_line(self):
		self.assertEqual(rejection_alert.get_sdi_error_reason("\nConnection refused\nretry later"), "Connection refused")
		self.assertEqual(rejection_alert.get_sdi_error_reason(None), "")

	def test_a_field_is_read_by_its_key(self):
		self.assertEqual(rejection_alert.get_sdi_error_field(NS_ERROR, "identificativo_sdi"), "18446768698")
		self.assertIsNone(rejection_alert.get_sdi_error_field(NS_ERROR, "missing"))

	def test_the_plain_alert_names_the_document_party_and_reason(self):
		with patch.object(rejection_alert, "get_url_to_form", return_value="https://erp/app/autofattura/AUTOFATT-0001"):
			subject, message = rejection_alert.build_rejection_alert(edi_document("rejected"))
		self.assertEqual(subject, "[SdI rejected] AUTOFATT/2026/00001 - Internet Domain Service BS Corp.")
		self.assertIn("18446768698", message)
		self.assertIn("N4892", message)
		self.assertIn('href="https://erp/app/autofattura/AUTOFATT-0001"', message)

	def test_the_configured_recipients_are_used(self):
		with patch.object(rejection_alert.frappe.db, "get_value", return_value="a@fabricators.ltd,\nb@fabricators.ltd"):
			self.assertEqual(
				rejection_alert.get_rejection_alert_recipients("FABRICATORS S.R.L."),
				["a@fabricators.ltd", "b@fabricators.ltd"],
			)
