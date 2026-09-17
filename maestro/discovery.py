"""P2P peer discovery for Maestro daemons (UDP multicast, stdlib only).

Each daemon joins a shared multicast group on the well-known UDP port
(SO_REUSEPORT lets several nodes share one host) and periodically announces
its presence; the kernel delivers every announcement to all local nodes in
the group. Peers are recorded in ``peers.json`` under the state dir and go
stale after three missed heartbeats. Manual registration
(``maestro peers add``) covers networks without multicast/broadcast.

Design notes:
- Announcements carry the node's HTTP port, pid, name, and a per-process
  nonce so a node ignores its own echoes (multicast loops back locally).
- The group/port are configurable; tests use TTL 0 on loopback so traffic
  never leaves the host. If the shared port cannot be bound at all, the node
  falls back to an ephemeral port (discovery degrades, daemon keeps running).
"""

from __future__ import annotations

import json
import os
import socket
import struct
import threading
import time
import uuid
from pathlib import Path
from typing import Any

DEFAULT_DISCOVERY_PORT = 9786
MULTICAST_GROUP = "234.5.6.7"  # private-use range
ANNOUNCE_INTERVAL_S = 5.0
STALE_AFTER_S = 15.0  # three missed heartbeats


def discovery_port_from_env() -> int:
    try:
        return int(os.environ.get("MAESTRO_DISCOVERY_PORT", DEFAULT_DISCOVERY_PORT))
    except ValueError:
        return DEFAULT_DISCOVERY_PORT


def discovery_enabled() -> bool:
    return os.environ.get("MAESTRO_DISCOVERY", "1").strip().lower() not in ("0", "false", "no", "off")


def discovery_interface_from_env() -> str:
    return os.environ.get("MAESTRO_DISCOVERY_IF", "0.0.0.0")


def discovery_ttl_from_env() -> int:
    try:
        return int(os.environ.get("MAESTRO_DISCOVERY_TTL", 1))
    except ValueError:
        return 1


class PeerTable:
    """The on-disk peer registry (peers.json) with staleness bookkeeping."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def load(self) -> dict[str, dict[str, Any]]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def save(self, peers: dict[str, dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(peers, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def upsert(self, peer: dict[str, Any]) -> None:
        key = str(peer.get("key") or "")
        if not key:
            return
        with self._lock:
            peers = self.load()
            entry = {k: v for k, v in peer.items() if k != "key"}
            entry["last_seen"] = time.time()
            existing = peers.get(key)
            if isinstance(existing, dict) and existing.get("manual"):
                # manually added peers keep their URL; refresh liveness only
                entry = {**existing, **{k: v for k, v in entry.items() if k not in ("name", "port")}, "last_seen": entry["last_seen"]}
            peers[key] = entry
            self.save(peers)

    def add_static(self, name: str, url: str) -> None:
        key = f"static:{name}"
        with self._lock:
            peers = self.load()
            peers[key] = {"name": name, "url": f"static:{url}", "port": None, "address": None, "last_seen": time.time(), "manual": True}
            self.save(peers)

    def remove(self, key_or_name: str) -> bool:
        with self._lock:
            peers = self.load()
            if key_or_name in peers:
                del peers[key_or_name]
                self.save(peers)
                return True
            for key in list(peers):
                if peers[key].get("name") == key_or_name or f"static:{key_or_name}" == key:
                    del peers[key]
                    self.save(peers)
                    return True
            return False

    def live(self, now: float | None = None) -> dict[str, dict[str, Any]]:
        """Peers seen within the staleness window (or manually added)."""
        now = now if now is not None else time.time()
        out: dict[str, dict[str, Any]] = {}
        for key, peer in self.load().items():
            last_seen = float(peer.get("last_seen") or 0)
            if peer.get("manual") or (now - last_seen) <= STALE_AFTER_S:
                out[key] = peer
        return out


class PresenceServer:
    """One daemon's UDP multicast presence: announce locally, record peers."""

    def __init__(
        self,
        http_port: int,
        table: PeerTable,
        *,
        name: str = "maestro-node",
        port: int | None = None,
        group: str = MULTICAST_GROUP,
        multicast_if: str = "0.0.0.0",
        ttl: int = 1,
        interval_s: float = ANNOUNCE_INTERVAL_S,
    ) -> None:
        self.http_port = http_port
        self.table = table
        self.name = name
        self.port = port if port is not None else discovery_port_from_env()
        self.group = group
        self.multicast_if = multicast_if
        self.ttl = ttl
        self.interval_s = interval_s
        self.nonce = uuid.uuid4().hex[:12]
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def _bind(self) -> bool:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if hasattr(socket, "SO_REUSEPORT"):
                try:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
                except OSError:
                    pass
            try:
                sock.bind(("", self.port))
            except OSError:
                # shared port unavailable on this host: fall back to an
                # ephemeral port (discovery degrades; manual peers still work)
                sock.close()
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind(("", 0))
            membership = struct.pack("4s4s", socket.inet_aton(self.group), socket.inet_aton(self.multicast_if))
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(self.multicast_if))
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, self.ttl)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
        except OSError:
            return False
        self._sock = sock
        return True

    def start(self) -> bool:
        """Bind + join the group + spawn the announce/listen loop.

        Returns False only if no UDP port could be bound at all (the daemon
        keeps running; manual ``peers add`` still works). The actual bound
        port is learned via ``self.port`` after start().
        """
        if self._thread is not None:
            return True
        if not self._bind():
            return False
        assert self._sock is not None
        # Learn the actual bound port (ephemeral when we asked for 0 or fell back).
        self.port = self._sock.getsockname()[1]
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="maestro-presence", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=2)
        self._thread = None
        self._sock = None

    def _announcement(self) -> bytes:
        return json.dumps(
            {"kind": "maestro-presence", "name": self.name, "http_port": self.http_port, "pid": os.getpid(), "nonce": self.nonce, "ts": time.time()},
            ensure_ascii=False,
        ).encode("utf-8")

    def _loop(self) -> None:
        assert self._sock is not None
        # Receive with a short timeout so the announce cadence does not depend
        # on incoming traffic (both nodes must be able to speak first).
        self._sock.settimeout(0.25)
        next_announce = 0.0
        while not self._stop.is_set():
            data = None
            try:
                data, addr = self._sock.recvfrom(2048)
            except socket.timeout:
                pass  # nothing to handle this tick; fall through to the announce check
            except OSError:
                break  # socket closed (stop()) or fatal error
            if data is not None:
                self._handle(data, addr)
            now = time.monotonic()
            if now >= next_announce:
                try:
                    self._sock.sendto(self._announcement(), (self.group, self.port))
                except OSError:
                    pass  # no route on this network; manual peers still work
                next_announce = now + self.interval_s

    def _handle(self, data: bytes, addr: tuple[str, int]) -> None:
        try:
            payload = json.loads(data.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return
        if not isinstance(payload, dict) or payload.get("kind") != "maestro-presence":
            return
        if payload.get("nonce") == self.nonce:
            return  # our own echo
        http_port = payload.get("http_port")
        if not isinstance(http_port, int):
            return
        name = str(payload.get("name") or f"node-{addr[0]}:{http_port}")
        self.table.upsert({"key": f"{addr[0]}:{http_port}", "name": name, "port": http_port, "address": addr[0], "url": f"http://{addr[0]}:{http_port}"})
