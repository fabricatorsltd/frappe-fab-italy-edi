from __future__ import annotations

from typing import Any

import frappe

from fab_italy_edi.channels.sdi_pec.base import SDIPECChannel
from fab_italy_edi.document_utils import normalize_identifier

HOOK_KEY = "fab_italy_edi_adapter_classes"
BUILTIN_ADAPTER_CLASSES = (SDIPECChannel,)


def get_adapter_class(adapter_key: str | None) -> type[Any] | None:
	normalized_key = normalize_identifier(adapter_key)
	if not normalized_key:
		return None

	for adapter_class in iter_adapter_classes():
		if getattr(adapter_class, "adapter_key", None) == normalized_key:
			return adapter_class
	return None


def get_provider_adapter(provider) -> Any | None:
	adapter_class = get_adapter_class(getattr(provider, "adapter_key", None))
	if not adapter_class:
		return None
	return adapter_class()


def iter_adapter_classes() -> list[type[Any]]:
	adapter_classes: list[type[Any]] = list(BUILTIN_ADAPTER_CLASSES)
	for dotted_path in frappe.get_hooks(HOOK_KEY) or []:
		adapter_class = frappe.get_attr(dotted_path)
		if adapter_class not in adapter_classes:
			adapter_classes.append(adapter_class)
	return adapter_classes
