from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import uuid4

from radiacode_app.reallocator import (
    KubernetesIdentity,
    delete_own_pod,
    read_reallocation_marker,
    run_reallocator,
    write_reallocation_marker,
)


class NonBlockingEvent:
    def is_set(self) -> bool:
        return False

    def wait(self, _: float) -> bool:
        return False


class ReallocatorTests(unittest.TestCase):
    def test_marker_is_atomic_private_and_bound_to_pod_uid(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "reallocate"
            connection_id = uuid4()
            write_reallocation_marker(path, pod_uid="pod-uid", connection_id=connection_id)
            raw = path.read_text(encoding="utf-8")
            self.assertNotIn("serial", raw.lower())
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            marker = read_reallocation_marker(path, expected_pod_uid="pod-uid")
            self.assertIsNotNone(marker)
            assert marker is not None
            self.assertEqual(marker.connection_id, str(connection_id))
            self.assertIsNone(read_reallocation_marker(path, expected_pod_uid="replacement-pod"))

    def test_reallocator_deletes_only_after_observing_same_valid_marker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "reallocate"
            identity = KubernetesIdentity("pod", "namespace", "uid", "kubernetes", 443)
            write_reallocation_marker(path, pod_uid="uid", connection_id=uuid4())
            deleted: list[KubernetesIdentity] = []
            run_reallocator(
                path,
                identity,
                wait_seconds=0,
                poll_seconds=0.001,
                stop_event=NonBlockingEvent(),  # type: ignore[arg-type]
                delete=deleted.append,
            )
            self.assertEqual(deleted, [identity])

    def test_delete_uses_uid_precondition_and_projected_token(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            token_path = root / "token"
            ca_path = root / "ca.crt"
            token_path.write_text("short-lived-token\n", encoding="utf-8")
            ca_path.write_text("not-read-by-mock", encoding="utf-8")
            identity = KubernetesIdentity(
                pod_name="collector-0",
                namespace="radiacode",
                pod_uid="uid-123",
                host="kubernetes.default.svc",
                port=443,
                token_path=token_path,
                ca_path=ca_path,
            )
            response = MagicMock(status=202)
            response.__enter__.return_value = response
            with (
                patch("radiacode_app.reallocator.ssl.create_default_context", return_value=object()),
                patch("radiacode_app.reallocator.urllib.request.urlopen", return_value=response) as open_url,
            ):
                delete_own_pod(identity)
            request = open_url.call_args.args[0]
            self.assertEqual(request.method, "DELETE")
            self.assertTrue(request.full_url.endswith("/namespaces/radiacode/pods/collector-0"))
            self.assertEqual(request.headers["Authorization"], "Bearer short-lived-token")
            body: dict[str, Any] = json.loads(request.data)
            self.assertEqual(body["preconditions"], {"uid": "uid-123"})


if __name__ == "__main__":
    unittest.main()
