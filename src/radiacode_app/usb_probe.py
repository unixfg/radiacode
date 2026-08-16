from __future__ import annotations

from collections.abc import Callable

from .device import RadiaCodeAdapter
from .settings import Settings


def probe_usb(
    settings: Settings,
    *,
    adapter_factory: Callable[[], RadiaCodeAdapter] | None = None,
) -> dict[str, object]:
    """Run non-destructive libusb checks under the production identity."""

    slug = settings.require_device_slug()
    serial = settings.require_device_serial()
    adapter = adapter_factory() if adapter_factory else RadiaCodeAdapter(serial)
    try:
        adapter.connect()
        result = adapter.probe()
    finally:
        adapter.close()
    observed_channels = result.get("channel_count")
    result["device"] = slug
    result["channel_count_valid"] = (
        isinstance(observed_channels, int)
        and 2 <= observed_channels <= 65_536
        and (settings.expected_channel_count is None or observed_channels == settings.expected_channel_count)
    )
    if not result["channel_count_valid"]:
        raise ValueError("device channel count did not match production configuration")
    return result
