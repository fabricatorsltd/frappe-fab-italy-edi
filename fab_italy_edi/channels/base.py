from __future__ import annotations

from typing import Any, Mapping


class ChannelAdapter:
	adapter_key = ""

	def validate_configuration(self, configuration: Mapping[str, Any]) -> list[str]:
		return []
