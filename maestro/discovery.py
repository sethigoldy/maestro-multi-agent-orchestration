"""P2P peer discovery for Maestro daemons (UDP multicast, stdlib only).

Each daemon joins a shared multicast group on the well-known UDP port
(SO_REUSEPORT lets several nodes share one host) and periodically announces
its presence; the kernel delivers every announcement to all local nodes in
the group. Peers are recorded in ``peers.json`` under the state dir and go
stale after three missed heartbeats. Manual registration
(``maestro peers add``) covers networks without multicast/broadcast.

Design notes:
- Announcements carry the node's HTTP port, the host it is reachable on
  (``http_host`` — its LAN IP when bound to all interfaces), pid, name, and a
  per-process nonce so a node ignores its own echoes (multicast loops back
  locally).
- The group/port are configurable; tests use TTL 0 on loopback so traffic
  never leaves the host. If the shared port cannot be bound at all, the node
  falls back to an ephemeral port (discovery degrades, daemon keeps running).
- The receive socket is bound to the multicast group address, not to every
  address, so a unicast packet sent straight to the port is never read. The
  send socket is bound to the discovery interface (``MAESTRO_DISCOVERY_IF``).
  When that interface is loopback, announcements from other hosts are ignored.
- Announcements are untrusted input. ``http_host`` must be an IP address,
  ``http_port`` must be a valid port, and names lose their control characters
  (so ``maestro peers list`` cannot be made to emit terminal escape codes).
  An announcement from another host may not name a loopback or link-local
  address (127.0.0.1, ::1, 169.254.169.254 and the like): a LAN host could
  otherwise point this machine at its own loopback services or at a cloud
  metadata endpoint. Only an announcement sent from a loopback address may
  name a loopback host.
- A node announces itself only on an interface that can reach the host it
  advertises. A daemon reachable only on loopback announces when the
  discovery interface is loopback too; on any other interface it listens for
  peers but never announces 127.0.0.1 to the network.
- ``peers.json`` holds at most ``MAX_PEERS`` entries: discovered peers unseen
  for ``PRUNE_AFTER_S`` are pruned and the oldest are dropped first; manually
  added peers are never dropped. The file is rewritten only when a peer is new
  or changed, or when its stored ``last_seen`` is older than
  ``LAST_SEEN_REFRESH_S`` — not on every packet.
- A daemon that listens on loopback only runs discovery only when
  ``MAESTRO_DISCOVERY`` is explicitly set to an on value.
"""

from __future__ import annotations

import ipaddress
import json
import os
import socket
import struct
import threading
import time
import unicodedata
import uuid
from pathlib import Path
from typing import Any

DEFAULT_DISCOVERY_PORT = 9786
MULTICAST_GROUP = "234.5.6.7"  # private-use range
ANNOUNCE_INTERVAL_S = 5.0
STALE_AFTER_S = 15.0  # three missed heartbeats
MAX_PEERS = 256  # peers.json never holds more entries than this
PRUNE_AFTER_S = 3600.0  # a discovered peer unseen for an hour is removed
LAST_SEEN_REFRESH_S = 5.0  # an unchanged peer's last_seen is rewritten at most this often
MAX_NAME_CHARS = 64
_ON_VALUES = ("1", "true", "yes", "on")
_OFF_VALUES = ("0", "false", "no", "off")


def discovery_port_from_env() -> int:
    try:
        return int(os.environ.get("MAESTRO_DISCOVERY_PORT", DEFAULT_DISCOVERY_PORT))
    except ValueError:
        return DEFAULT_DISCOVERY_PORT


def discovery_enabled(loopback_only: bool = False) -> bool:
    """Whether a daemon should announce itself and listen for peers.

    ``MAESTRO_DISCOVERY=0`` (or false, no, off) always turns discovery off. A
    daemon that listens on loopback only cannot be reached from another
    machine, so for it discovery runs only when ``MAESTRO_DISCOVERY`` is set to
    an on value (1, true, yes, on). A daemon that listens beyond loopback runs
    discovery unless it is turned off.
    """
    raw = os.environ.get("MAESTRO_DISCOVERY", "").strip().lower()
    if raw in _OFF_VALUES:
        return False
    if loopback_only:
        return raw in _ON_VALUES
    return True


def clean_text(value: str, limit: int | None = None) -> str:
    """Remove control and format characters (terminal escapes, bidi overrides), then cut to ``limit``."""
    cleaned = "".join(ch for ch in value if unicodedata.category(ch) not in ("Cc", "Cf", "Zl", "Zp"))
    return cleaned[:limit] if limit is not None else cleaned


def _is_loopback(address: str) -> bool:
    try:
        return ipaddress.ip_address(address).is_loopback
    except ValueError:
        return False


def _is_local_only(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True for an address that only makes sense on the machine or link it came from."""
    return ip.is_loopback or ip.is_link_local


def _url_host(host: str, sender: str) -> str | None:
    """``host`` formatted for a URL, or None when it is not a usable IP address.

    ``sender`` is the address the announcement came from. A loopback or
    link-local host is usable only when the sender itself is on loopback, that
    is, when the announcement came from this machine.
    """
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return None
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped  # ::ffff:127.0.0.1 is 127.0.0.1
    if ip.is_multicast or ip.is_unspecified:
        return None
    if _is_local_only(ip) and not _is_loopback(sender):
        return None
    return f"[{ip}]" if ip.version == 6 else str(ip)


def _announces_host(http_host: str, multicast_if: str) -> bool:
    """Whether a node advertising ``http_host`` should announce on ``multicast_if``.

    Peers on the network drop a loopback or link-local host sent by another
    machine, and it would be wrong for them anyway, so such a host is
    announced only on a loopback interface, where only this machine hears it.
    """
    if _is_loopback(multicast_if):
        return True
    try:
        ip = ipaddress.ip_address(http_host)
    except ValueError:
        return True  # not an address at all: receivers drop it on their own
    return not _is_local_only(ip)


def discovery_interface_from_env() -> str:
    return os.environ.get("MAESTRO_DISCOVERY_IF", "0.0.0.0")


def discovery_ttl_from_env() -> int:
    try:
        return int(os.environ.get("MAESTRO_DISCOVERY_TTL", 1))
    except ValueError:
        return 1


def pick_lan_ip() -> str:
    """Best-effort primary non-loopback IPv4 of this host.

    Uses the classic connect-UDP-to-a-public-address trick (no packet is
    actually sent) so the result matches the interface a LAN peer would use
    to reach us. Falls back to 127.0.0.1 when no route exists (air-gapped,
    container without default route) — discovery still works on loopback.
    """
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(("8.8.8.8", 80))
            ip = probe.getsockname()[0]
        finally:
            probe.close()
    except OSError:
        return "127.0.0.1"
    return ip or "127.0.0.1"


class PeerTable:
    """The on-disk peer registry (peers.json) with staleness bookkeeping."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def load(self) -> dict[str, dict[str, Any]]:
        """The stored peers, cleaned: entries that are not objects are dropped and
        names and URLs lose control characters (older versions stored them raw)."""
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        peers: dict[str, dict[str, Any]] = {}
        for key, peer in data.items():
            if not isinstance(peer, dict):
                continue
            entry = dict(peer)
            for field in ("name", "url"):
                if isinstance(entry.get(field), str):
                    entry[field] = clean_text(entry[field])
            peers[clean_text(key)] = entry
        return peers

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
            now = time.time()
            entry = {k: v for k, v in peer.items() if k != "key"}
            entry["last_seen"] = now
            existing = peers.get(key)
            if existing is not None and existing.get("manual"):
                # manually added peers keep their URL; refresh liveness only
                entry = {**existing, **{k: v for k, v in entry.items() if k not in ("name", "port")}, "last_seen": now}
            if (
                existing is not None
                and _without_last_seen(existing) == _without_last_seen(entry)
                and now - float(existing.get("last_seen") or 0) < LAST_SEEN_REFRESH_S
            ):
                return  # the same announcement again, recently recorded: nothing to write
            peers[key] = entry
            _prune(peers, now)
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


def _without_last_seen(peer: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in peer.items() if k != "last_seen"}


def _prune(peers: dict[str, dict[str, Any]], now: float) -> None:
    """Drop discovered peers unseen for PRUNE_AFTER_S, then the oldest ones beyond MAX_PEERS.

    Manually added peers are never dropped.
    """
    for key in [k for k, p in peers.items() if not p.get("manual") and now - float(p.get("last_seen") or 0) > PRUNE_AFTER_S]:
        del peers[key]
    discovered = sorted((k for k, p in peers.items() if not p.get("manual")), key=lambda k: float(peers[k].get("last_seen") or 0))
    for key in discovered[: max(len(peers) - MAX_PEERS, 0)]:
        del peers[key]


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
        http_host: str = "127.0.0.1",
    ) -> None:
        self.http_port = http_port
        self.table = table
        self.name = name
        self.port = port if port is not None else discovery_port_from_env()
        self.group = group
        self.multicast_if = multicast_if
        self.ttl = ttl
        self.interval_s = interval_s
        self.http_host = http_host
        self.nonce = uuid.uuid4().hex[:12]
        self._loopback_only = _is_loopback(multicast_if)
        # False for a loopback-only daemon on a network interface: it still
        # records peers it hears, but never sends 127.0.0.1 to the network.
        self.announces = _announces_host(http_host, multicast_if)
        self._sock: socket.socket | None = None  # receives group traffic
        self._send_sock: socket.socket | None = None  # sends announcements
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def _receive_socket(self, port: int) -> socket.socket:
        """A UDP socket bound to the multicast group address on ``port``.

        Binding to the group rather than to every address means the socket
        only reads packets sent to the group, never unicast packets aimed
        straight at the port.
        """
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if hasattr(socket, "SO_REUSEPORT"):
                try:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
                except OSError:
                    pass
            sock.bind((self.group, port))
        except OSError:
            sock.close()
            raise
        return sock

    def _bind(self) -> bool:
        try:
            try:
                sock = self._receive_socket(self.port)
            except OSError:
                # shared port unavailable on this host: fall back to an
                # ephemeral port (discovery degrades; manual peers still work)
                sock = self._receive_socket(0)
        except OSError:
            return False
        send: socket.socket | None = None
        try:
            membership = struct.pack("4s4s", socket.inet_aton(self.group), socket.inet_aton(self.multicast_if))
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)
            # A socket bound to a group address cannot send, so announcements
            # leave through a second socket bound to the discovery interface.
            send = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            if self.multicast_if != "0.0.0.0":
                send.bind((self.multicast_if, 0))
            send.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(self.multicast_if))
            send.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, self.ttl)
            send.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
        except OSError:
            sock.close()
            if send is not None:
                send.close()
            return False
        self._sock = sock
        self._send_sock = send
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
        for sock in (self._sock, self._send_sock):
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
        if self._thread is not None:
            self._thread.join(timeout=2)
        self._thread = None
        self._sock = None
        self._send_sock = None

    def _announcement(self) -> bytes:
        return json.dumps(
            {
                "kind": "maestro-presence",
                "name": self.name,
                "http_port": self.http_port,
                "http_host": self.http_host,
                "pid": os.getpid(),
                "nonce": self.nonce,
                "ts": time.time(),
            },
            ensure_ascii=False,
        ).encode("utf-8")

    def _loop(self) -> None:
        assert self._sock is not None and self._send_sock is not None
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
            if self.announces and now >= next_announce:
                try:
                    self._send_sock.sendto(self._announcement(), (self.group, self.port))
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
        if self._loopback_only and not _is_loopback(addr[0]):
            return  # a loopback listener only accepts peers on this machine
        http_port = payload.get("http_port")
        if not isinstance(http_port, int) or isinstance(http_port, bool) or not 0 < http_port < 65536:
            return
        # Newer nodes advertise the host they are reachable on (a daemon bound
        # to 0.0.0.0 announces its LAN IP); older ones omit it — fall back to
        # the multicast source address, which is right for same-host peers.
        host = payload.get("http_host")
        if not isinstance(host, str) or not host:
            host = addr[0]
        url_host = _url_host(host, addr[0])
        if url_host is None:
            return  # not a usable IP address for this sender: never record a URL built from it
        raw_name = payload.get("name")
        name = clean_text(raw_name, MAX_NAME_CHARS) if isinstance(raw_name, str) else ""
        name = name or f"node-{addr[0]}:{http_port}"
        self.table.upsert({"key": f"{addr[0]}:{http_port}", "name": name, "port": http_port, "address": addr[0], "url": f"http://{url_host}:{http_port}"})
