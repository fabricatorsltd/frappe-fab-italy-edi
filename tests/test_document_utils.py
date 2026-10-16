import unittest
from types import SimpleNamespace
from unittest.mock import patch

from frappe.exceptions import ValidationError

from fab_italy_edi import document_utils


class TestSurfaceValidationErrors(unittest.TestCase):
	def raise_validation_error(self):
		raise ValidationError("Set the foreign supplier Tax ID.")

	def test_shows_a_bare_validation_error(self):
		fn = document_utils.surface_validation_errors(self.raise_validation_error)
		with (
			patch.object(document_utils.frappe, "local", SimpleNamespace(message_log=[])),
			patch.object(document_utils.frappe, "msgprint") as msgprint,
		):
			with self.assertRaises(ValidationError):
				fn()
		msgprint.assert_called_once()
		self.assertEqual(msgprint.call_args.args[0], "Set the foreign supplier Tax ID.")

	def test_keeps_an_existing_message(self):
		fn = document_utils.surface_validation_errors(self.raise_validation_error)
		with (
			patch.object(document_utils.frappe, "local", SimpleNamespace(message_log=["already shown"])),
			patch.object(document_utils.frappe, "msgprint") as msgprint,
		):
			with self.assertRaises(ValidationError):
				fn()
		msgprint.assert_not_called()
