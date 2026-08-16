from __future__ import annotations

import json
import os
import ssl
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from uuid import UUID


@dataclass(frozen=True, slots=True)
class ReallocationMarker:
    version: int
    reason: str
    pod_uid: str
    connection_id: str
    created_at: str


def write_reallocation_marker(path: Path, *, pod_uid: str, connection_id: UUID) -> None:
    """Atomically request reallocation for the sole permitted USB failure."""

    marker = ReallocationMarker(
        version=1,
        reason="libusb_no_device",
        pod_uid=pod_uid,
        connection_id=str(connection_id),
        created_at=datetime.now(UTC).isoformat(),
    )
    path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".reallocate-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(asdict(marker), stream, separators=(",", ":"), sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def read_reallocation_marker(path: Path, *, expected_pod_uid: str) -> ReallocationMarker | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    expected_keys = {"version", "reason", "pod_uid", "connection_id", "created_at"}
    if not isinstance(raw, dict) or set(raw) != expected_keys:
        return None
    try:
        marker = ReallocationMarker(**raw)
        UUID(marker.connection_id)
        created_at = datetime.fromisoformat(marker.created_at)
    except (TypeError, ValueError):
        return None
    if created_at.tzinfo is None or created_at.utcoffset() is None:
        return None
    if marker.version != 1 or marker.reason != "libusb_no_device" or marker.pod_uid != expected_pod_uid:
        return None
    return marker


@dataclass(frozen=True, slots=True)
class KubernetesIdentity:
    pod_name: str
    namespace: str
    pod_uid: str
    host: str
    port: int
    token_path: Path = Path("/var/run/secrets/kubernetes.io/serviceaccount/token")
    ca_path: Path = Path("/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")

    @classmethod
    def from_environment(cls) -> KubernetesIdentity:
        return cls(
            pod_name=os.environ["POD_NAME"],
            namespace=os.environ["POD_NAMESPACE"],
            pod_uid=os.environ["POD_UID"],
            host=os.environ["KUBERNETES_SERVICE_HOST"],
            port=int(os.environ.get("KUBERNETES_SERVICE_PORT_HTTPS", "443")),
        )


def delete_own_pod(identity: KubernetesIdentity) -> None:
    token = identity.token_path.read_text(encoding="utf-8").strip()
    namespace = urllib.parse.quote(identity.namespace, safe="")
    pod_name = urllib.parse.quote(identity.pod_name, safe="")
    url = f"https://{identity.host}:{identity.port}/api/v1/namespaces/{namespace}/pods/{pod_name}"
    body = json.dumps(
        {
            "apiVersion": "v1",
            "kind": "DeleteOptions",
            "preconditions": {"uid": identity.pod_uid},
            "propagationPolicy": "Background",
        },
        separators=(",", ":"),
    ).encode()
    request = urllib.request.Request(
        url,
        method="DELETE",
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    context = ssl.create_default_context(cafile=identity.ca_path)
    try:
        with urllib.request.urlopen(request, context=context, timeout=10) as response:
            if response.status not in {200, 202}:
                raise RuntimeError("Kubernetes pod deletion was not accepted")
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise RuntimeError("Kubernetes pod deletion failed") from None


def run_reallocator(
    marker_path: Path,
    identity: KubernetesIdentity,
    *,
    wait_seconds: float = 20.0,
    poll_seconds: float = 1.0,
    stop_event: Event | None = None,
    delete: Callable[[KubernetesIdentity], None] = delete_own_pod,
) -> None:
    if wait_seconds < 0 or poll_seconds <= 0:
        raise ValueError("invalid reallocator timing")
    stop = stop_event or Event()
    observed_marker: ReallocationMarker | None = None
    observed_at = 0.0
    while not stop.is_set():
        marker = read_reallocation_marker(marker_path, expected_pod_uid=identity.pod_uid)
        if marker is None:
            observed_marker = None
        elif marker != observed_marker:
            observed_marker = marker
            observed_at = time.monotonic()
        elif time.monotonic() - observed_at >= wait_seconds:
            delete(identity)
            return
        stop.wait(poll_seconds)
