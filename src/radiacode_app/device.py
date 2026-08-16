from __future__ import annotations

import errno as errno_module
import hmac
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from .models import DeviceSpectrum


class USBFailureClass(StrEnum):
    NO_DEVICE = "no_device"
    ACCESS_DENIED = "access_denied"
    BUSY = "busy"
    TIMEOUT = "timeout"
    NOT_FOUND = "not_found"
    CONNECTION_CLOSED = "connection_closed"
    OTHER = "other"


class DeviceIdentityError(RuntimeError):
    pass


class SerializedAccessError(RuntimeError):
    pass


_BACKEND_CODES = {
    -4: USBFailureClass.NO_DEVICE,
    -3: USBFailureClass.ACCESS_DENIED,
    -6: USBFailureClass.BUSY,
    -7: USBFailureClass.TIMEOUT,
}
_ERRNO_CODES = {
    errno_module.ENODEV: USBFailureClass.NO_DEVICE,
    errno_module.EACCES: USBFailureClass.ACCESS_DENIED,
    errno_module.EBUSY: USBFailureClass.BUSY,
    errno_module.ETIMEDOUT: USBFailureClass.TIMEOUT,
}


def classify_usb_error(error: BaseException) -> USBFailureClass:
    """Classify PyUSB/libusb failures by numeric codes, never message text."""

    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if current.__class__.__name__ == "DeviceNotFound":
            return USBFailureClass.NOT_FOUND
        if current.__class__.__name__ == "ConnectionClosed":
            return USBFailureClass.CONNECTION_CLOSED
        backend_code = getattr(current, "backend_error_code", None)
        if backend_code in _BACKEND_CODES:
            return _BACKEND_CODES[backend_code]
        os_errno = getattr(current, "errno", None)
        if os_errno in _ERRNO_CODES:
            return _ERRNO_CODES[os_errno]
        current = current.__cause__ or current.__context__
    return USBFailureClass.OTHER


def _default_factory(*, serial_number: str) -> Any:
    from radiacode import RadiaCode  # type: ignore[import-untyped]

    return RadiaCode(serial_number=serial_number)


class RadiaCodeAdapter:
    """Single-thread-owned adapter over radiacode 0.4.0's synchronous API."""

    def __init__(
        self,
        device_serial: str,
        *,
        factory: Callable[..., Any] = _default_factory,
    ) -> None:
        self._device_serial = device_serial
        self._factory = factory
        self._client: Any | None = None
        self._owner_thread: int | None = None

    def __repr__(self) -> str:
        return "RadiaCodeAdapter(serial=[redacted])"

    def _claim_or_assert_owner(self) -> None:
        current = threading.get_ident()
        if self._owner_thread is None:
            self._owner_thread = current
        elif self._owner_thread != current:
            raise SerializedAccessError("all device operations must run on the owning thread")

    def _connected_client(self) -> Any:
        self._claim_or_assert_owner()
        if self._client is None:
            raise RuntimeError("device is not connected")
        return self._client

    def connect(self) -> None:
        self._claim_or_assert_owner()
        if self._client is not None:
            raise RuntimeError("device is already connected")
        client = self._factory(serial_number=self._device_serial)
        try:
            observed_serial = client.serial_number()
            if not hmac.compare_digest(observed_serial, self._device_serial):
                raise DeviceIdentityError("connected USB device did not match the configured identity")
        except BaseException:
            client.close()
            raise
        self._client = client

    def close(self) -> None:
        self._claim_or_assert_owner()
        client, self._client = self._client, None
        if client is not None:
            client.close()

    def read_data_buf_raw(self) -> bytes:
        """Consume DATA_BUF and return the undecoded response payload.

        `VS` and `read_request` are intentionally pinned to radiacode 0.4.0 by
        the lockfile and contract tests. Calling `data_buf()` here would lose
        unknown records, truncation bytes, sequence values, and raw ticks.
        """

        from radiacode.types import VS  # type: ignore[import-untyped]

        response = self._connected_client().read_request(VS.DATA_BUF)
        return bytes(response.data())

    def read_spectrum(self, *, observed_at: datetime | None = None) -> DeviceSpectrum:
        spectrum = self._connected_client().spectrum()
        return self._to_spectrum(spectrum, observed_at)

    def read_accumulated_spectrum(self, *, observed_at: datetime | None = None) -> DeviceSpectrum:
        spectrum = self._connected_client().spectrum_accum()
        return self._to_spectrum(spectrum, observed_at)

    @staticmethod
    def _to_spectrum(spectrum: Any, observed_at: datetime | None) -> DeviceSpectrum:
        timestamp = observed_at or datetime.now(UTC)
        duration = spectrum.duration.total_seconds()
        if not duration.is_integer():
            raise ValueError("device returned a fractional spectrum duration")
        return DeviceSpectrum(
            observed_at=timestamp,
            duration_seconds=int(duration),
            coefficients=(float(spectrum.a0), float(spectrum.a1), float(spectrum.a2)),
            counts=tuple(int(value) for value in spectrum.counts),
        )

    def probe(self) -> dict[str, object]:
        """Return diagnostics safe for internal logs; never read destructive DATA_BUF."""

        client = self._connected_client()
        boot, target = client.fw_version()
        spectrum = self.read_spectrum()
        return {
            "connected": True,
            "firmware": {
                "boot_major": boot[0],
                "boot_minor": boot[1],
                "target_major": target[0],
                "target_minor": target[1],
            },
            "channel_count": len(spectrum.counts),
            "spectrum_duration_seconds": spectrum.duration_seconds,
            "calibration": spectrum.coefficients,
        }
