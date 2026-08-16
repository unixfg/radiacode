from __future__ import annotations

import errno
import threading
import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

from radiacode_app.device import (
    DeviceIdentityError,
    RadiaCodeAdapter,
    SerializedAccessError,
    USBFailureClass,
    classify_usb_error,
)


class FakeClient:
    def __init__(self, serial: str = "configured-secret") -> None:
        self.serial = serial
        self.closed = False
        self.requests: list[object] = []
        self.destructive_reads = 0

    def serial_number(self) -> str:
        return self.serial

    def close(self) -> None:
        self.closed = True

    def read_request(self, variable: object) -> Any:
        self.destructive_reads += 1
        self.requests.append(variable)
        return SimpleNamespace(data=lambda: bytearray(b"raw-buffer"))

    def spectrum(self) -> Any:
        return SimpleNamespace(
            duration=timedelta(seconds=42),
            a0=1.0,
            a1=2.0,
            a2=3.0,
            counts=[4, 5, 6],
        )

    def spectrum_accum(self) -> Any:
        return self.spectrum()

    def fw_version(self) -> tuple[tuple[int, int], tuple[int, int]]:
        return (1, 2), (3, 4)


class USBError(RuntimeError):
    def __init__(self, *, backend_error_code: int | None = None, os_errno: int | None = None) -> None:
        super().__init__("message text is deliberately irrelevant")
        self.backend_error_code = backend_error_code
        self.errno = os_errno


class DeviceTests(unittest.TestCase):
    def test_numeric_usb_error_classification_and_chaining(self) -> None:
        self.assertEqual(classify_usb_error(USBError(backend_error_code=-4)), USBFailureClass.NO_DEVICE)
        self.assertEqual(classify_usb_error(USBError(backend_error_code=-3)), USBFailureClass.ACCESS_DENIED)
        self.assertEqual(classify_usb_error(USBError(backend_error_code=-6)), USBFailureClass.BUSY)
        self.assertEqual(classify_usb_error(USBError(backend_error_code=-7)), USBFailureClass.TIMEOUT)
        self.assertEqual(classify_usb_error(USBError(os_errno=errno.ENODEV)), USBFailureClass.NO_DEVICE)
        try:
            try:
                raise USBError(os_errno=errno.EACCES)
            except USBError as cause:
                raise RuntimeError("wrapper") from cause
        except RuntimeError as wrapped:
            self.assertEqual(classify_usb_error(wrapped), USBFailureClass.ACCESS_DENIED)

    def test_adapter_validates_identity_and_redacts_representation(self) -> None:
        wrong_client = FakeClient("different-secret")
        adapter = RadiaCodeAdapter(
            "configured-secret",
            factory=lambda **_: wrong_client,
        )
        self.assertNotIn("configured-secret", repr(adapter))
        with self.assertRaises(DeviceIdentityError):
            adapter.connect()
        self.assertTrue(wrong_client.closed)

    def test_raw_request_and_probe_keep_data_buf_out_of_probe(self) -> None:
        client = FakeClient()
        adapter = RadiaCodeAdapter("configured-secret", factory=lambda **_: client)
        adapter.connect()
        result = adapter.probe()
        self.assertEqual(result["channel_count"], 3)
        self.assertEqual(client.destructive_reads, 0)
        self.assertEqual(adapter.read_data_buf_raw(), b"raw-buffer")
        self.assertEqual(client.destructive_reads, 1)
        observed_at = datetime(2026, 8, 16, tzinfo=UTC)
        spectrum = adapter.read_spectrum(observed_at=observed_at)
        self.assertEqual(spectrum.observed_at, observed_at)
        self.assertEqual(spectrum.duration_seconds, 42)
        self.assertEqual(spectrum.counts, (4, 5, 6))
        adapter.close()

    def test_cross_thread_commands_are_rejected(self) -> None:
        client = FakeClient()
        adapter = RadiaCodeAdapter("configured-secret", factory=lambda **_: client)
        adapter.connect()
        failures: list[BaseException] = []

        def operate() -> None:
            try:
                adapter.read_spectrum()
            except BaseException as error:
                failures.append(error)

        worker = threading.Thread(target=operate)
        worker.start()
        worker.join()
        self.assertEqual(len(failures), 1)
        self.assertIsInstance(failures[0], SerializedAccessError)
        adapter.close()


if __name__ == "__main__":
    unittest.main()
