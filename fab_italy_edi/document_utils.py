from __future__ import annotations

from typing import Any, Mapping


def normalize_identifier(value: Any) -> str | None:
	if value is None:
		return None
	text = str(value).strip()
	return text or None


def normalize_vat_code(value: Any) -> str | None:
	identifier = normalize_identifier(value)
	if not identifier:
		return None
	if len(identifier) > 2 and identifier[:2].isalpha():
		return identifier[2:]
	return identifier


def coerce_bool(value: Any, *, default: bool) -> bool:
	if value is None:
		return default
	if isinstance(value, bool):
		return value
	if isinstance(value, (int, float)):
		return bool(value)
	text = str(value).strip().lower()
	if text in {"1", "true", "yes", "y", "on"}:
		return True
	if text in {"0", "false", "no", "n", "off"}:
		return False
	return default


def get_document_value(document: Mapping[str, Any] | Any, fieldname: str) -> Any:
	if isinstance(document, Mapping):
		return document.get(fieldname)
	getter = getattr(document, "get", None)
	if callable(getter):
		return getter(fieldname)
	return getattr(document, fieldname, None)


def get_document_secret(document: Mapping[str, Any] | Any, fieldname: str) -> Any:
	get_password = getattr(document, "get_password", None)
	if callable(get_password):
		return get_password(fieldname, raise_exception=False)
	return get_document_value(document, fieldname)
