from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any


def get_value(source: Any, fieldname: str, default=None):
	if isinstance(source, dict):
		return source.get(fieldname, default)

	return getattr(source, fieldname, default)


def to_decimal(value: Any) -> Decimal | None:
	if value in (None, ""):
		return None

	return Decimal(str(value))


@dataclass(slots=True)
class CanonicalParty:
	name: str
	tax_id: str | None = None
	recipient_code: str | None = None
	pec_address: str | None = None


@dataclass(slots=True)
class ProcurementReference:
	reference_type: str
	document_id: str | None = None
	document_date: date | None = None
	cig: str | None = None
	cup: str | None = None


@dataclass(slots=True)
class CanonicalInvoice:
	source_doctype: str
	source_name: str
	document_kind: str
	company: str
	party: CanonicalParty
	posting_date: date | None = None
	due_date: date | None = None
	currency: str | None = None
	grand_total: Decimal | None = None
	remarks: str | None = None
	procurement_references: list[ProcurementReference] = field(default_factory=list)


def build_procurement_references(source: Any) -> list[ProcurementReference]:
	references: list[ProcurementReference] = []
	order_reference = ProcurementReference(
		reference_type="purchase_order",
		document_id=get_value(source, "po_no"),
		document_date=get_value(source, "po_date"),
		cig=get_value(source, "fab_edi_cig"),
		cup=get_value(source, "fab_edi_cup"),
	)

	if any(
		[
			order_reference.document_id,
			order_reference.document_date,
			order_reference.cig,
			order_reference.cup,
		]
	):
		references.append(order_reference)

	return references


def from_sales_invoice(source: Any) -> CanonicalInvoice:
	return CanonicalInvoice(
		source_doctype="Sales Invoice",
		source_name=get_value(source, "name"),
		document_kind="credit_note" if get_value(source, "is_return") else "invoice",
		company=get_value(source, "company"),
		party=CanonicalParty(
			name=get_value(source, "customer_name") or get_value(source, "customer"),
			tax_id=get_value(source, "tax_id"),
		),
		posting_date=get_value(source, "posting_date"),
		due_date=get_value(source, "due_date"),
		currency=get_value(source, "currency"),
		grand_total=to_decimal(get_value(source, "grand_total")),
		remarks=get_value(source, "remarks"),
		procurement_references=build_procurement_references(source),
	)


def from_purchase_invoice(source: Any) -> CanonicalInvoice:
	return CanonicalInvoice(
		source_doctype="Purchase Invoice",
		source_name=get_value(source, "name"),
		document_kind="supplier_invoice_import",
		company=get_value(source, "company"),
		party=CanonicalParty(
			name=get_value(source, "supplier_name") or get_value(source, "supplier"),
			tax_id=get_value(source, "tax_id"),
		),
		posting_date=get_value(source, "posting_date"),
		due_date=get_value(source, "due_date"),
		currency=get_value(source, "currency"),
		grand_total=to_decimal(get_value(source, "grand_total")),
		remarks=get_value(source, "remarks"),
		procurement_references=build_procurement_references(source),
	)
