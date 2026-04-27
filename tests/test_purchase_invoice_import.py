from __future__ import annotations

import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import frappe

from fab_italy_edi import purchase_invoice_import


SAMPLE_SUPPLIER_XML = """<?xml version="1.0" encoding="UTF-8"?>
<FatturaElettronica versione="FPR12" xmlns="http://ivaservizi.agenziaentrate.gov.it/docs/xsd/fatture/v1.2">
	<FatturaElettronicaHeader>
		<DatiTrasmissione>
			<ProgressivoInvio>467</ProgressivoInvio>
			<CodiceDestinatario>K95IV18</CodiceDestinatario>
		</DatiTrasmissione>
		<CedentePrestatore>
			<DatiAnagrafici>
				<IdFiscaleIVA>
					<IdPaese>IT</IdPaese>
					<IdCodice>12454611000</IdCodice>
				</IdFiscaleIVA>
				<Anagrafica>
					<Denominazione>Verisure Italy Srl</Denominazione>
				</Anagrafica>
			</DatiAnagrafici>
			<Sede>
				<Indirizzo>Viale dell'Oceano Pacifico</Indirizzo>
				<NumeroCivico>171/173</NumeroCivico>
				<CAP>00144</CAP>
				<Comune>Roma</Comune>
				<Provincia>RM</Provincia>
				<Nazione>IT</Nazione>
			</Sede>
			<Contatti>
				<Email>billing@example.it</Email>
			</Contatti>
		</CedentePrestatore>
	</FatturaElettronicaHeader>
	<FatturaElettronicaBody>
		<DatiGenerali>
			<DatiGeneraliDocumento>
				<TipoDocumento>TD01</TipoDocumento>
				<Divisa>EUR</Divisa>
				<Data>2026-04-01</Data>
				<Numero>2604C0354215</Numero>
				<ImportoTotaleDocumento>72.94</ImportoTotaleDocumento>
				<Causale>Imported from sample</Causale>
			</DatiGeneraliDocumento>
		</DatiGenerali>
		<DatiBeniServizi>
			<DettaglioLinee>
				<NumeroLinea>1</NumeroLinea>
				<Descrizione>KIT BASE 1 un.</Descrizione>
				<Quantita>1.00</Quantita>
				<PrezzoUnitario>34.90</PrezzoUnitario>
				<PrezzoTotale>34.90</PrezzoTotale>
				<AliquotaIVA>22.00</AliquotaIVA>
			</DettaglioLinee>
			<DatiRiepilogo>
				<AliquotaIVA>22.00</AliquotaIVA>
				<ImponibileImporto>59.79</ImponibileImporto>
				<Imposta>13.15</Imposta>
				<RiferimentoNormativo>Iva Impon. 22%</RiferimentoNormativo>
			</DatiRiepilogo>
		</DatiBeniServizi>
		<DatiPagamento>
			<DettaglioPagamento>
				<ModalitaPagamento>MP20</ModalitaPagamento>
				<DataScadenzaPagamento>2026-04-30</DataScadenzaPagamento>
				<ImportoPagamento>72.94</ImportoPagamento>
				<IBAN>IT74G0306903293100000018562</IBAN>
			</DettaglioPagamento>
		</DatiPagamento>
	</FatturaElettronicaBody>
</FatturaElettronica>
"""


class TestPurchaseInvoiceImport(unittest.TestCase):
	def test_parse_supplier_invoice_source_extracts_preview_from_xml(self):
		with patch.object(
			purchase_invoice_import,
			"frappe",
			new=SimpleNamespace(
				db=SimpleNamespace(get_value=Mock(return_value="Italy")),
				defaults=SimpleNamespace(get_global_default=Mock(return_value="EUR")),
			),
		):
			preview = purchase_invoice_import.parse_supplier_invoice_source(SAMPLE_SUPPLIER_XML)

		self.assertEqual(preview["supplier"]["display_name"], "Verisure Italy Srl")
		self.assertEqual(preview["supplier"]["tax_id"], "IT12454611000")
		self.assertEqual(preview["invoice"]["bill_no"], "2604C0354215")
		self.assertEqual(preview["invoice"]["due_date"], "2026-04-30")
		self.assertEqual(preview["invoice"]["total_amount"], 72.94)
		self.assertEqual(preview["taxes"][0]["tax_amount"], 13.15)
		self.assertEqual(preview["payments"][0]["iban"], "IT74G0306903293100000018562")

	def test_build_item_preview_prefers_codice_tipo_and_riferimento_testo(self):
		item = purchase_invoice_import.build_item_preview(
			{
				"numero_linea": "1",
				"codice_articolo": [
					{
						"codice_tipo": "PRESTAZIONE",
						"codice_valore": "CONSULEN",
					}
				],
				"descrizione": "CONSULENZA",
				"quantita": "1.00",
				"prezzo_unitario": "50.000000",
				"prezzo_totale": "50.00",
				"aliquota_iva": "22.00",
				"altri_dati_gestionali": [
					{
						"tipo_dato": "NOTA",
						"riferimento_testo": "ISTANZA DI RATEIZZAZIONE CARTELLE - MIRKO BROMBIN",
					}
				],
			}
		)

		self.assertEqual(item["item_name"], "PRESTAZIONE")
		self.assertEqual(item["description"], "ISTANZA DI RATEIZZAZIONE CARTELLE - MIRKO BROMBIN")
		self.assertEqual(item["notes"], ["CONSULENZA"])

	def test_build_purchase_invoice_taxes_requires_account_when_tax_exists(self):
		with self.assertRaises(frappe.ValidationError):
			purchase_invoice_import.build_purchase_invoice_taxes(
				{"taxes": [{"description": "VAT 22%", "tax_amount": 13.15}]}
			)

	def test_build_purchase_invoice_taxes_uses_company_mapping(self):
		configuration = SimpleNamespace(
			get=Mock(
				return_value=[
					SimpleNamespace(tax_rate=22.0, nature=None, account_head="IVA 22% - fab"),
					SimpleNamespace(tax_rate=10.0, nature=None, account_head="IVA 10% - fab"),
				]
			)
		)
		with patch.object(
			purchase_invoice_import,
			"frappe",
			new=SimpleNamespace(
				db=SimpleNamespace(exists=Mock(return_value=True)),
				get_cached_doc=Mock(return_value=configuration),
			),
		), patch.object(
			purchase_invoice_import,
			"ensure_inbound_natura_account_enabled",
			side_effect=lambda account_head, nature=None: account_head,
		):
			tax_rows, unresolved = purchase_invoice_import.build_purchase_invoice_taxes(
				{
					"taxes": [
						{"description": "VAT 22%", "tax_amount": 13.15, "tax_rate": 22.0},
						{"description": "VAT 10%", "tax_amount": 5.0, "tax_rate": 10.0},
					]
				},
				company="Fabricators",
			)

		self.assertEqual(unresolved, [])
		self.assertEqual(
			tax_rows,
			[
				{
					"charge_type": "Actual",
					"account_head": "IVA 22% - fab",
					"description": "VAT 22%",
					"rate": 22.0,
					"tax_amount": 13.15,
					"included_in_print_rate": 0,
					"dont_recompute_tax": 1,
				},
				{
					"charge_type": "Actual",
					"account_head": "IVA 10% - fab",
					"description": "VAT 10%",
					"rate": 10.0,
					"tax_amount": 5.0,
					"included_in_print_rate": 0,
					"dont_recompute_tax": 1,
				},
			],
		)

	def test_build_purchase_invoice_taxes_reports_unmapped_buckets(self):
		with patch.object(
			purchase_invoice_import,
			"frappe",
			new=SimpleNamespace(db=SimpleNamespace(exists=Mock(return_value=False))),
		):
			tax_rows, unresolved = purchase_invoice_import.build_purchase_invoice_taxes(
				{"taxes": [{"description": "VAT 22%", "tax_amount": 13.15, "tax_rate": 22.0}]},
				company="Fabricators",
				allow_unmapped=True,
			)

		self.assertEqual(tax_rows, [])
		self.assertEqual(
			unresolved,
			[
				{
					"tax_rate": 22.0,
					"nature": None,
					"description": "VAT 22%",
					"label": "22.00%",
				}
			],
		)

	def test_build_purchase_invoice_taxes_keeps_zero_natura_bucket(self):
		configuration = SimpleNamespace(
			get=Mock(
				return_value=[
					SimpleNamespace(tax_rate=0.0, nature="N2.2", account_head="Fuori campo IVA N2.2 - fab"),
				]
			)
		)
		with patch.object(
			purchase_invoice_import,
			"frappe",
			new=SimpleNamespace(
				db=SimpleNamespace(exists=Mock(return_value=True)),
				get_cached_doc=Mock(return_value=configuration),
			),
		), patch.object(
			purchase_invoice_import,
			"ensure_inbound_natura_account_enabled",
			side_effect=lambda account_head, nature=None: account_head,
		):
			tax_rows, unresolved = purchase_invoice_import.build_purchase_invoice_taxes(
				{
					"taxes": [
						{
							"description": "Fuori campo IVA (N2.2)",
							"tax_amount": 0.0,
							"tax_rate": 0.0,
							"nature": "N2.2",
						}
					]
				},
				company="Fabricators",
			)

		self.assertEqual(unresolved, [])
		self.assertEqual(
			tax_rows,
			[
				{
					"charge_type": "Actual",
					"account_head": "Fuori campo IVA N2.2 - fab",
					"description": "Fuori campo IVA (N2.2)",
					"rate": 0.0,
					"tax_amount": 0.0,
					"included_in_print_rate": 0,
					"dont_recompute_tax": 1,
				}
			],
		)

	def test_build_inbound_item_wise_tax_details_respects_bucket_bindings(self):
		details = purchase_invoice_import.build_inbound_item_wise_tax_details(
			{
				"items": [
					{
						"item_name": "Room",
						"amount": 54.55,
						"tax_rate": 10.0,
						"nature": None,
					},
					{
						"item_name": "Tourist tax",
						"amount": 1.50,
						"tax_rate": 0.0,
						"nature": "N2.2",
					},
				]
			},
			[
				{
					"source_tax": {
						"description": "VAT 10%",
						"taxable_amount": 54.55,
						"tax_amount": 5.45,
						"tax_rate": 10.0,
						"nature": None,
					}
				},
				{
					"source_tax": {
						"description": "Fuori campo IVA (N2.2)",
						"taxable_amount": 1.50,
						"tax_amount": 0.0,
						"tax_rate": 0.0,
						"nature": "N2.2",
					}
				},
			],
		)

		self.assertEqual(
			details,
			[
				{
					"item_index": 0,
					"tax_index": 0,
					"rate": 10.0,
					"amount": 5.45,
					"taxable_amount": 54.55,
				},
				{
					"item_index": 1,
					"tax_index": 1,
					"rate": 0.0,
					"amount": 0.0,
					"taxable_amount": 1.5,
				},
			],
		)

	def test_build_purchase_invoice_items_uses_company_defaults(self):
		with (
			patch.object(purchase_invoice_import, "get_default_uom", return_value="Nos"),
			patch.object(purchase_invoice_import, "get_default_expense_account", return_value="5111 - Cost of Goods Sold - fab"),
			patch.object(purchase_invoice_import, "get_default_cost_center", return_value="Main - fab"),
			patch.object(purchase_invoice_import, "ensure_uom", return_value="Nos"),
			patch.object(purchase_invoice_import, "get_inbound_tax_mapping_rows", return_value=[]),
			patch.object(purchase_invoice_import, "get_inbound_item_tax_template_for_item", return_value="Inbound 22.00% - fab"),
		):
			rows = purchase_invoice_import.build_purchase_invoice_items(
				{
					"items": [
						{
							"item_name": "KIT BASE 1 un.",
							"description": "KIT BASE 1 un.",
							"qty": 1,
							"uom": "Nos",
							"rate": 34.9,
							"amount": 34.9,
						}
					]
				},
				company="Fabricators",
			)

		self.assertEqual(rows[0]["expense_account"], "5111 - Cost of Goods Sold - fab")
		self.assertEqual(rows[0]["cost_center"], "Main - fab")
		self.assertEqual(rows[0]["item_tax_template"], "Inbound 22.00% - fab")

	def test_create_purchase_invoice_draft_creates_native_review_draft(self):
		document = SimpleNamespace(
			name="EDI-DOC-0001",
			company="Fabricators",
			source_xml="/private/files/sample.xml",
			transmission_state="ready",
			last_error="old error",
			save=Mock(),
			add_comment=Mock(),
			source_doctype="EDI Configuration",
			source_name="fabricators",
		)
		preview = {
			"invoice": {
				"bill_no": "2604C0354215",
				"bill_date": "2026-04-01",
				"due_date": "2026-04-30",
				"currency": "EUR",
				"is_return": False,
				"total_net_amount": 59.79,
				"total_tax_amount": 13.15,
				"total_amount": 72.94,
			},
			"supplier": {"display_name": "Verisure Italy Srl"},
			"default_supplier": None,
			"items": [{"item_name": "KIT BASE 1 un.", "description": "KIT BASE 1 un.", "qty": 1, "uom": "Unit", "rate": 34.9, "amount": 34.9}],
			"taxes": [{"description": "VAT 22%", "tax_amount": 13.15}],
			"payments": [{"mode": "MP20", "iban": "IT74G0306903293100000018562"}],
		}
		pi = SimpleNamespace(name="ACC-PINV-2026-00001", insert=Mock())
		frappe_stub = SimpleNamespace(
			has_permission=Mock(),
			db=SimpleNamespace(exists=Mock(return_value=False)),
			get_doc=Mock(return_value=pi),
		)

		with (
			patch.object(purchase_invoice_import, "get_incoming_supplier_document", return_value=document),
			patch.object(purchase_invoice_import, "find_linked_purchase_invoice", return_value=None),
			patch.object(purchase_invoice_import, "build_incoming_supplier_invoice_preview", return_value=preview),
			patch.object(
				purchase_invoice_import,
				"build_purchase_invoice_items",
				return_value=[{"item_name": "KIT BASE 1 un."}],
			),
			patch.object(
				purchase_invoice_import,
				"build_purchase_invoice_taxes",
				return_value=([], []),
			),
			patch.object(purchase_invoice_import, "prepare_purchase_invoice_exact_taxes"),
			patch.object(purchase_invoice_import, "frappe", new=frappe_stub),
		):
			result = purchase_invoice_import.create_purchase_invoice_draft_from_edi_document(
				"EDI-DOC-0001"
			)

		self.assertEqual(result, {"purchase_invoice": "ACC-PINV-2026-00001", "created": True})
		document.save.assert_called_once_with(ignore_permissions=True)
		document.add_comment.assert_called_once()
		pi.insert.assert_called_once_with(ignore_permissions=True, ignore_mandatory=True)
		self.assertEqual(frappe_stub.get_doc.call_args.args[0]["fab_edi_document"], "EDI-DOC-0001")
		self.assertEqual(frappe_stub.get_doc.call_args.args[0]["supplier_name"], "Verisure Italy Srl")
		self.assertEqual(document.source_doctype, "Purchase Invoice")
		self.assertEqual(document.source_name, "ACC-PINV-2026-00001")

	def test_get_incoming_supplier_document_blocks_quarantined_placeholder(self):
		document = SimpleNamespace(
			name="EDI-DOC-0009",
			document_kind="supplier_invoice_import",
			last_error=(
				f"{purchase_invoice_import.QUARANTINED_INBOUND_SUPPLIER_PREFIX}: "
				"collided with Sales Invoice ACC-SINV-2026-00001."
			),
		)
		frappe_stub = SimpleNamespace(
			get_doc=Mock(return_value=document),
			has_permission=Mock(),
		)

		with patch.object(purchase_invoice_import, "frappe", new=frappe_stub):
			with self.assertRaises(frappe.ValidationError):
				purchase_invoice_import.get_incoming_supplier_document("EDI-DOC-0009")

	def test_materialize_inbound_supplier_links_creates_missing_records(self):
		doc = SimpleNamespace(
			doctype="Supplier",
			name="SUP-0001",
			supplier_name="Verisure Italy Srl",
			supplier_type="Company",
			supplier_primary_contact=None,
			supplier_primary_address=None,
			default_bank_account=None,
			email_id=None,
			mobile_no=None,
			country=None,
			primary_address=None,
			fab_edi_supplier_preview_json=json.dumps(
				{
					"email": "billing@example.it",
					"phone": "+3906123456",
					"address_line1": "Viale dell'Oceano Pacifico 171/173",
					"city": "Roma",
					"province": "RM",
					"pincode": "00144",
					"country": "Italy",
				}
			),
			fab_edi_payments_preview_json=json.dumps(
				[{"iban": "IT74G0306903293100000018562", "bank_name": "Imported Bank"}]
			),
			get=lambda fieldname, default=None: getattr(doc, fieldname, default),
			set=lambda fieldname, value: setattr(doc, fieldname, value),
			db_set=Mock(side_effect=lambda fieldname, value, *args, **kwargs: setattr(doc, fieldname, value)),
		)

		contact = SimpleNamespace(name="CONT-0001")
		address = SimpleNamespace(name="ADDR-0001")
		bank_account = SimpleNamespace(name="BANK-ACC-0001", insert=Mock())

		def get_doc_side_effect(payload):
			if payload["doctype"] == "Bank Account":
				return bank_account
			if payload["doctype"] == "Bank":
				return SimpleNamespace(name="Imported Bank", insert=Mock(return_value=SimpleNamespace(name="Imported Bank")))
			self.fail(f"Unexpected doctype {payload['doctype']}")

		frappe_stub = SimpleNamespace(
			db=SimpleNamespace(
				get_value=Mock(return_value=None),
				set_value=Mock(),
			),
			get_doc=Mock(side_effect=get_doc_side_effect),
			_dict=lambda value: value,
		)

		with (
			patch.object(purchase_invoice_import, "frappe", new=frappe_stub),
			patch.object(purchase_invoice_import, "resolve_bank_name_from_abi", return_value="INTESA SANPAOLO SPA"),
			patch("erpnext.selling.doctype.customer.customer.make_contact", return_value=contact),
			patch("erpnext.selling.doctype.customer.customer.make_address", return_value=address),
			patch(
				"frappe.contacts.doctype.address.address.get_address_display",
				return_value="Viale dell'Oceano Pacifico 171/173\n00144 Roma\nItaly",
			),
		):
			purchase_invoice_import.materialize_inbound_supplier_links(doc)

		self.assertEqual(doc.supplier_primary_contact, "CONT-0001")
		self.assertEqual(doc.supplier_primary_address, "ADDR-0001")
		self.assertEqual(doc.default_bank_account, "BANK-ACC-0001")
		self.assertEqual(doc.country, "Italy")
		self.assertIsNone(doc.fab_edi_supplier_preview_json)
		self.assertIsNone(doc.fab_edi_payments_preview_json)
		bank_account.insert.assert_called_once_with(ignore_permissions=True)
		bank_account_payload = next(
			call.args[0]
			for call in frappe_stub.get_doc.call_args_list
			if call.args and isinstance(call.args[0], dict) and call.args[0].get("doctype") == "Bank Account"
		)
		self.assertEqual(
			bank_account_payload["bank_account_no"],
			"100000018562",
		)
		self.assertEqual(
			bank_account_payload["branch_code"],
			"03293",
		)

	def test_parse_iban_details_extracts_italian_components(self):
		details = purchase_invoice_import.parse_iban_details("IT74 G03069 03293 100000018562")

		self.assertEqual(details["iban"], "IT74G0306903293100000018562")
		self.assertEqual(details["abi_code"], "03069")
		self.assertEqual(details["branch_code"], "03293")
		self.assertEqual(details["bank_account_no"], "100000018562")

	def test_normalize_existing_bank_account_updates_bank_from_imported_directory(self):
		class StubBankAccount(SimpleNamespace):
			def get(self, fieldname):
				return getattr(self, fieldname, None)

			def set(self, fieldname, value):
				setattr(self, fieldname, value)

		bank_account = StubBankAccount(
			bank="Imported EDI Bank",
			branch_code=None,
			bank_account_no=None,
			save=Mock(),
		)

		with (
			patch.object(
				purchase_invoice_import,
				"frappe",
				new=SimpleNamespace(get_doc=Mock(return_value=bank_account)),
			),
			patch.object(
				purchase_invoice_import,
				"get_bank_record_from_abi",
				return_value={
					"name": "INTESA SANPAOLO SPA",
					"bank_name": "INTESA SANPAOLO SPA",
					"fab_abi_code": "03069",
				},
			),
		):
			purchase_invoice_import.normalize_existing_bank_account(
				"BANK-ACC-0001",
				{
					"abi_code": "03069",
					"branch_code": "03293",
					"bank_account_no": "100000018562",
				},
			)

		self.assertEqual(bank_account.bank, "INTESA SANPAOLO SPA")
		self.assertEqual(bank_account.branch_code, "03293")
		self.assertEqual(bank_account.bank_account_no, "100000018562")
		bank_account.save.assert_called_once_with(ignore_permissions=True)
