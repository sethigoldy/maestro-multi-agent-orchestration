"""v2-M4: P2P peer discovery — PeerTable, PresenceServer, and the peers CLI."""

from __future__ import annotations

import json
import os
import socket
import time
from pathlib import Path

import pytest

from maestro.discovery import (
    MULTICAST_GROUP,
    STALE_AFTER_S,
    PeerTable,
    PresenceServer,
    discovery_enabled,
    discovery_port_from_env,
)


# ------------------------------------------------------------- PeerTable

def test_peer_table_roundtrip_and_staleness(tmp_path):
    table = PeerTable(tmp_path / "peers.json")
    assert table.load() == {}  # missing file
    (tmp_path / "peers.json").write_text("{not json", encoding="utf-8")
    assert table.load() == {}  # malformed file

    table.upsert({"key": "10.0.0.2:8790", "name": "node-b", "port": 8790, "address": "10.0.0.2"})
    peers = table.load()
    assert peers["10.0.0.2:8790"]["name"] == "node-b"
    assert "last_seen" in peers["10.0.0.2:8790"]

    # fresh peer is live; aged peer is stale
    now = time.time()
    assert set(table.live(now)) == {"10.0.0.2:8790"}
    assert set(table.live(now + STALE_AFTER_S + 1)) == set()


def test_peer_table_upsert_without_key_is_noop(tmp_path):
    table = PeerTable(tmp_path / "peers.json")
    table.upsert({"name": "no-key"})
    assert table.load() == {}


def test_peer_table_static_peers_survive_and_persist(tmp_path):
    table = PeerTable(tmp_path / "peers.json")
    table.add_static("lab", "http://10.0.0.9:8790")
    peer = table.load()["static:lab"]
    assert peer["url"] == "static:http://10.0.0.9:8790" and peer["manual"] is True

    # manual peers are never stale, even after a long silence
    assert set(table.live(time.time() + 10_000)) == {"static:lab"}

    # a broadcast announcement for the same name must not clobber the static URL
    table.upsert({"key": "static:lab", "name": "renamed", "port": 9999, "address": "10.9.9.9"})
    again = table.load()["static:lab"]
    assert again["url"] == "static:http://10.0.0.9:8790" and again["manual"] is True


def test_peer_table_remove_by_key_and_name(tmp_path):
    table = PeerTable(tmp_path / "peers.json")
    table.add_static("lab", "http://10.0.0.9:8790")
    assert table.remove("static:lab") is True
    assert table.load() == {}
    table.add_static("other", "http://10.0.0.8:8790")
    assert table.remove("other") is True  # by name
    assert table.remove("missing") is False


# -------------------------------------------------------- PresenceServer

def _ephemeral_udp_port() -> int:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    probe.bind(("", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def test_presence_two_nodes_discover_each_other(tmp_path):
    """Two presence servers on one host must find each other (multicast loopback)."""
    # both nodes share one UDP port (SO_REUSEPORT fans the group out to all of them)
    port = _ephemeral_udp_port()
    table_a = PeerTable(tmp_path / "a" / "peers.json")
    table_b = PeerTable(tmp_path / "b" / "peers.json")
    server_a = PresenceServer(8001, table_a, name="node-a", port=port, multicast_if="127.0.0.1", ttl=0, interval_s=0.2)
    server_b = PresenceServer(8002, table_b, name="node-b", port=port, multicast_if="127.0.0.1", ttl=0, interval_s=0.2)
    assert server_a.start() and server_b.start()
    try:
        deadline = time.monotonic() + 5
        seen_a = seen_b = False
        while time.monotonic() < deadline and not (seen_a and seen_b):
            peers_a, peers_b = table_a.live(), table_b.live()
            seen_a = any(p.get("name") == "node-b" for p in peers_a.values())
            seen_b = any(p.get("name") == "node-a" for p in peers_b.values())
            if not (seen_a and seen_b):
                time.sleep(0.1)
        assert seen_a, f"node-a never saw node-b: {table_a.load()}"
        assert seen_b, f"node-b never saw node-a: {table_b.load()}"
        entry = next(p for p in table_a.live().values() if p.get("name") == "node-b")
        assert entry["port"] == 8002 and entry["url"] == "http://127.0.0.1:8002"
    finally:
        server_a.stop()
        server_b.stop()


def test_presence_ignores_foreign_and_own_traffic(tmp_path):
    port = _ephemeral_udp_port()
    table = PeerTable(tmp_path / "peers.json")
    server = PresenceServer(8001, table, name="node-a", port=port, multicast_if="127.0.0.1", ttl=0, interval_s=60)
    assert server.start()
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        # non-announcement garbage and a wrong-kind payload are ignored (sent to the group;
        # the joined socket receives them via loopback delivery)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 0)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton("127.0.0.1"))
        sock.sendto(b"not json at all", (MULTICAST_GROUP, port))
        sock.sendto(json.dumps({"kind": "other"}).encode(), (MULTICAST_GROUP, port))
        # an announcement missing http_port is ignored
        sock.sendto(json.dumps({"kind": "maestro-presence", "name": "x"}).encode(), (MULTICAST_GROUP, port))
        time.sleep(0.5)
        assert table.load() == {}
    finally:
        server.stop()
        sock.close()


def test_presence_bind_failure(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise OSError("port busy")

    table = PeerTable(tmp_path / "peers.json")
    server = PresenceServer(8001, table, name="node-a", port=1)
    monkeypatch.setattr(socket.socket, "bind", boom)
    assert server.start() is False


def test_presence_stop_is_idempotent_and_safe(tmp_path):
    table = PeerTable(tmp_path / "peers.json")
    server = PresenceServer(8001, table, name="node-a", port=_ephemeral_udp_port(), multicast_if="127.0.0.1", ttl=0)
    assert server.start()
    server.stop()
    server.stop()  # idempotent


# ------------------------------------------------------------- env knobs

def test_discovery_env_knobs(monkeypatch):
    monkeypatch.setenv("MAESTRO_DISCOVERY_PORT", "12345")
    assert discovery_port_from_env() == 12345
    monkeypatch.setenv("MAESTRO_DISCOVERY_PORT", "garbage")
    assert discovery_port_from_env() == 9786
    for value in ("0", "false", "off"):
        monkeypatch.setenv("MAESTRO_DISCOVERY", value)
        assert discovery_enabled() is False
    monkeypatch.delenv("MAESTRO_DISCOVERY")
    assert discovery_enabled() is True


# ------------------------------------------------------------------- CLI

def _peers_cli(tmp_path, monkeypatch, *argv: str) -> int:
    from maestro import cli as clic

    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("MAESTRO_HOME", str(home))
    return clic.main(list(argv))


def test_peers_cli_add_list_remove(tmp_path, monkeypatch):
    rc = _peers_cli(tmp_path, monkeypatch, "peers", "add", "--name", "lab", "--url", "http://10.0.0.9:8790")
    assert rc == 0
    table = PeerTable(Path(maestro_home(tmp_path)) / "peers.json")
    assert table.load()["static:lab"]["url"] == "static:http://10.0.0.9:8790"

    import io
    import contextlib

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = _peers_cli(tmp_path, monkeypatch, "peers", "list")
    assert rc == 0 and "static:http://10.0.0.9:8790" in out.getvalue() and "manual" in out.getvalue()

    rc = _peers_cli(tmp_path, monkeypatch, "peers", "remove", "lab")
    assert rc == 0 and table.load() == {}
    rc = _peers_cli(tmp_path, monkeypatch, "peers", "remove", "nope")
    assert rc == 1


def test_peers_cli_bad_url_and_empty_list(tmp_path, monkeypatch):
    import contextlib
    import io

    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        rc = _peers_cli(tmp_path, monkeypatch, "peers", "add", "--name", "x", "--url", "ftp://nope")
    assert rc == 2 and "http(s)" in err.getvalue()

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = _peers_cli(tmp_path, monkeypatch, "peers", "list")
    assert rc == 0 and "no peers" in out.getvalue()


def maestro_home(tmp_path) -> str:
    return str(tmp_path / "home")


# -------------------------------------------------- daemon integration

def test_daemon_starts_presence_and_writes_peers(tmp_path, monkeypatch):
    from maestro.daemon import MaestroDaemon

    home = tmp_path / "home"
    monkeypatch.setenv("MAESTRO_HOME", str(home))
    port = _ephemeral_udp_port()
    monkeypatch.setenv("MAESTRO_DISCOVERY_PORT", str(port))
    monkeypatch.setenv("MAESTRO_DISCOVERY", "1")  # a loopback daemon announces only when asked to
    d = MaestroDaemon(state_dir=home, start_http=True, port=0, max_retries=0, backoff_s=0)
    try:
        assert d._presence is not None and d._presence.port == port
        # the presence loop announces itself; our own echo is filtered, so no self-entry appears
        time.sleep(0.6)
        table = PeerTable(home / "peers.json")
        for peer in table.load().values():
            assert peer.get("name") != "maestro-node" or peer.get("url") != f"http://127.0.0.1:{d.port}"
    finally:
        d.stop()


def test_daemon_discovery_disabled(tmp_path, monkeypatch):
    from maestro.daemon import MaestroDaemon

    home = tmp_path / "home"
    monkeypatch.setenv("MAESTRO_HOME", str(home))
    monkeypatch.setenv("MAESTRO_DISCOVERY", "0")
    d = MaestroDaemon(state_dir=home, start_http=True, port=0, max_retries=0, backoff_s=0)
    try:
        assert d._presence is None
    finally:
        d.stop()


# ------------------------------------------------- branch-completeness tests

def test_ttl_env_garbage_falls_back(tmp_path, monkeypatch):
    from maestro.discovery import discovery_ttl_from_env

    monkeypatch.setenv("MAESTRO_DISCOVERY_TTL", "garbage")
    assert discovery_ttl_from_env() == 1


def test_peer_table_remove_skips_nonmatching_keys(tmp_path):
    table = PeerTable(tmp_path / "peers.json")
    table.add_static("first", "http://10.0.0.1:8790")
    table.add_static("second", "http://10.0.0.2:8790")
    assert table.remove("second") is True  # first key fails the name match, second hits
    assert set(table.load()) == {"static:first"}


def test_presence_start_twice_is_idempotent(tmp_path):
    table = PeerTable(tmp_path / "peers.json")
    server = PresenceServer(8001, table, name="node-a", port=_ephemeral_udp_port(), multicast_if="127.0.0.1", ttl=0)
    assert server.start() and server.start()  # second call returns early
    server.stop()


def test_presence_reuseport_absent_and_setsockopt_failure(tmp_path, monkeypatch):
    import socket as _socket

    table = PeerTable(tmp_path / "peers.json")

    # platform without SO_REUSEPORT: the hasattr branch is skipped entirely
    reuseport_value = _socket.SO_REUSEPORT
    monkeypatch.delattr(_socket, "SO_REUSEPORT")
    server = PresenceServer(8001, table, name="node-a", port=_ephemeral_udp_port(), multicast_if="127.0.0.1", ttl=0)
    assert server.start()
    server.stop()

    # SO_REUSEPORT present but setsockopt refuses it: the inner except passes on
    monkeypatch.setattr(_socket, "SO_REUSEPORT", reuseport_value, raising=False)  # delattr above persists until teardown
    real_setsockopt = _socket.socket.setsockopt

    def picky(self, level, optname, value):
        if optname == getattr(_socket, "SO_REUSEPORT", -1):
            raise OSError("no reuseport here")
        return real_setsockopt(self, level, optname, value)

    monkeypatch.setattr(_socket.socket, "setsockopt", picky)
    server = PresenceServer(8001, table, name="node-a", port=_ephemeral_udp_port(), multicast_if="127.0.0.1", ttl=0)
    assert server.start()
    server.stop()


def test_presence_bind_falls_back_to_ephemeral(tmp_path, monkeypatch):
    import socket as _socket

    real_bind = _socket.socket.bind

    def busy(self, addr):
        if addr == (MULTICAST_GROUP, 12345):
            raise OSError("address in use")
        return real_bind(self, addr)

    monkeypatch.setattr(_socket.socket, "bind", busy)
    table = PeerTable(tmp_path / "peers.json")
    server = PresenceServer(8001, table, name="node-a", port=12345, multicast_if="127.0.0.1", ttl=0)
    assert server.start()
    try:
        assert server.port != 12345  # fell back to an ephemeral port
    finally:
        server.stop()


class _FakePresenceSocket:
    """Scripted socket for driving PresenceServer._loop / stop() deterministically."""

    def __init__(self, recv_results=None):
        self.recv_results = list(recv_results or [])
        self.sent: list[tuple[bytes, tuple[str, int]]] = []
        self.closed = False
        self.send_error: Exception | None = None

    def settimeout(self, _s):
        pass

    def getsockname(self):
        return ("127.0.0.1", 1)

    def recvfrom(self, _n):
        if not self.recv_results:
            raise OSError("socket closed")
        item = self.recv_results.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def sendto(self, data, addr):
        if self.send_error is not None:
            raise self.send_error
        self.sent.append((data, addr))

    def close(self):
        if self.closed and self.close_error is not None:
            raise self.close_error
        self.closed = True

    close_error: Exception | None = None


def test_presence_loop_send_failure_and_recv_error(tmp_path):
    table = PeerTable(tmp_path / "peers.json")
    # A LAN listener (not loopback), because the scripted announcement comes from 10.0.0.9.
    server = PresenceServer(8001, table, name="node-a", port=1, multicast_if="0.0.0.0", ttl=0)

    # sendto fails (no route) but the loop keeps running and records the peer
    sock = _FakePresenceSocket(recv_results=[((b'{"kind": "maestro-presence", "name": "remote", "http_port": 9001, "pid": 42, "nonce": "abc"}', ("10.0.0.9", 9)))])
    sock.send_error = OSError("no route to host")
    server._sock = server._send_sock = sock
    server._loop()  # runs one tick: recv ok, send fails silently, then stops on the next recv error
    assert table.load().get("10.0.0.9:9001", {}).get("name") == "remote"

    # recvfrom raises OSError: the loop breaks cleanly
    server2 = PresenceServer(8001, PeerTable(tmp_path / "p.json"), name="node-a", port=1, multicast_if="127.0.0.1", ttl=0)
    sock2 = _FakePresenceSocket(recv_results=[OSError("socket closed")])
    server2._sock = server2._send_sock = sock2
    server2._loop()  # must return without raising


def test_presence_stop_close_failure(tmp_path):
    table = PeerTable(tmp_path / "peers.json")
    server = PresenceServer(8001, table, name="node-a", port=_ephemeral_udp_port(), multicast_if="127.0.0.1", ttl=0)
    assert server.start()
    real_sock = server._sock

    class _Flaky:
        def __init__(self, inner):
            self.inner = inner

        def close(self):
            raise OSError("close failed")

        def __getattr__(self, name):
            return getattr(self.inner, name)

    server._sock = _Flaky(real_sock)
    server.stop()  # must swallow the close error and still join the thread


def test_daemon_presence_start_failure_is_nonfatal(tmp_path, monkeypatch):
    from maestro import discovery as disc
    from maestro.daemon import MaestroDaemon

    home = tmp_path / "home"
    monkeypatch.setenv("MAESTRO_HOME", str(home))

    def no_start(self):
        return False

    monkeypatch.setattr(disc.PresenceServer, "start", no_start)
    monkeypatch.setenv("MAESTRO_DISCOVERY", "1")  # so the start attempt really happens
    d = MaestroDaemon(state_dir=home, start_http=True, port=0, max_retries=0, backoff_s=0)
    try:
        assert d._presence is None  # daemon still fully functional without presence
        assert d.port > 0
    finally:
        d.stop()


def test_handle_ignores_garbage_directly(tmp_path):
    table = PeerTable(tmp_path / "peers.json")
    server = PresenceServer(8001, table, name="node-a", port=1, multicast_if="127.0.0.1", ttl=0)
    server._handle(b"not json at all", ("10.0.0.9", 9))
    server._handle(json.dumps({"kind": "other"}).encode(), ("10.0.0.9", 9))
    server._handle(json.dumps({"kind": "maestro-presence", "name": "x"}).encode(), ("10.0.0.9", 9))
    assert table.load() == {}


# ------------------------------------------------------------- http_host awareness

def test_announcement_carries_http_host(tmp_path):
    table = PeerTable(tmp_path / "peers.json")
    server = PresenceServer(8790, table, name="node-a", port=1, multicast_if="127.0.0.1", ttl=0, http_host="192.0.2.44")
    payload = json.loads(server._announcement())
    assert payload["http_host"] == "192.0.2.44" and payload["http_port"] == 8790


def test_handle_uses_advertised_host_with_source_fallback(tmp_path):
    table = PeerTable(tmp_path / "peers.json")
    # A LAN listener (not loopback), because the announcements come from 10.0.0.9.
    server = PresenceServer(8001, table, name="node-a", port=1, multicast_if="0.0.0.0", ttl=1)
    # newer nodes advertise the host they are reachable on (LAN IP for 0.0.0.0 binds)
    server._handle(json.dumps({"kind": "maestro-presence", "name": "b", "http_port": 8790, "http_host": "192.0.2.44"}).encode(), ("10.0.0.9", 9))
    peer = table.load()["10.0.0.9:8790"]
    assert peer["url"] == "http://192.0.2.44:8790" and peer["address"] == "10.0.0.9"
    # older nodes omit http_host — fall back to the multicast source address
    server._handle(json.dumps({"kind": "maestro-presence", "name": "c", "http_port": 8791}).encode(), ("10.0.0.9", 9))
    assert table.load()["10.0.0.9:8791"]["url"] == "http://10.0.0.9:8791"
    # empty or non-string http_host also falls back
    server._handle(json.dumps({"kind": "maestro-presence", "name": "d", "http_port": 8792, "http_host": ""}).encode(), ("10.0.0.9", 9))
    assert table.load()["10.0.0.9:8792"]["url"] == "http://10.0.0.9:8792"
    server._handle(json.dumps({"kind": "maestro-presence", "name": "e", "http_port": 8793, "http_host": 42}).encode(), ("10.0.0.9", 9))
    assert table.load()["10.0.0.9:8793"]["url"] == "http://10.0.0.9:8793"


def test_pick_lan_ip_falls_back_on_socket_error(monkeypatch):
    import maestro.discovery as disc

    def boom(*a, **kw):
        raise OSError("no route")

    monkeypatch.setattr(disc.socket, "socket", boom)
    assert disc.pick_lan_ip() == "127.0.0.1"


def test_pick_lan_ip_success_path(monkeypatch):
    import maestro.discovery as disc

    class _FakeProbe:
        def __init__(self, *a, **kw):
            pass

        def connect(self, addr):
            self.addr = addr  # UDP "connect" — no packet sent

        def getsockname(self):
            return ("192.0.2.77", 0)

        def close(self):
            pass

    monkeypatch.setattr(disc.socket, "socket", _FakeProbe)
    assert disc.pick_lan_ip() == "192.0.2.77"


# ----------------------------------------------- hardening: binding, validation, limits

def test_presence_receives_only_group_traffic_not_unicast(tmp_path):
    """The receive socket is bound to the multicast group, so a unicast packet
    sent straight to the port is never read, and the send socket is bound to
    the discovery interface."""
    port = _ephemeral_udp_port()
    table = PeerTable(tmp_path / "peers.json")
    server = PresenceServer(8001, table, name="node-a", port=port, multicast_if="127.0.0.1", ttl=0, interval_s=60)
    assert server.start()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        assert server._sock.getsockname()[0] == MULTICAST_GROUP
        assert server._send_sock.getsockname()[0] == "127.0.0.1"
        announcement = {"kind": "maestro-presence", "name": "injected", "http_port": 9001, "nonce": "zzz"}
        sock.sendto(json.dumps(announcement).encode(), ("127.0.0.1", port))
        time.sleep(0.5)
        assert table.load() == {}
    finally:
        sock.close()
        server.stop()


def test_presence_send_socket_is_unbound_for_the_default_interface(tmp_path):
    server = PresenceServer(8001, PeerTable(tmp_path / "peers.json"), name="node-a", port=_ephemeral_udp_port(), multicast_if="0.0.0.0", ttl=0)
    try:
        assert server._bind() is True
        assert server._send_sock.getsockname()[0] == "0.0.0.0"
    finally:
        server.stop()


def test_presence_send_socket_failure_closes_the_receive_socket(tmp_path, monkeypatch):
    import socket as _socket

    real_bind = _socket.socket.bind

    def refuse_send_bind(self, addr):
        if addr == ("127.0.0.1", 0):
            raise OSError("cannot bind the send socket")
        return real_bind(self, addr)

    monkeypatch.setattr(_socket.socket, "bind", refuse_send_bind)
    server = PresenceServer(8001, PeerTable(tmp_path / "peers.json"), name="node-a", port=_ephemeral_udp_port(), multicast_if="127.0.0.1", ttl=0)
    assert server.start() is False
    assert server._sock is None and server._send_sock is None


def test_presence_with_an_invalid_interface_does_not_start(tmp_path):
    """A MAESTRO_DISCOVERY_IF that is not an address fails the group join; both sockets are closed."""
    server = PresenceServer(8001, PeerTable(tmp_path / "peers.json"), name="node-a", port=_ephemeral_udp_port(), multicast_if="not-an-address", ttl=0)
    assert server.start() is False
    assert server._sock is None and server._send_sock is None


def test_loopback_listener_ignores_announcements_from_other_hosts(tmp_path):
    table = PeerTable(tmp_path / "peers.json")
    server = PresenceServer(8001, table, name="node-a", port=1, multicast_if="127.0.0.1", ttl=0)
    announcement = json.dumps({"kind": "maestro-presence", "name": "lan", "http_port": 8790}).encode()
    server._handle(announcement, ("192.0.2.9", 9))
    assert table.load() == {}
    server._handle(announcement, ("127.0.0.1", 9))
    assert set(table.load()) == {"127.0.0.1:8790"}
    # A listener on a real interface accepts peers from the network.
    lan = PresenceServer(8001, table, name="node-a", port=1, multicast_if="0.0.0.0", ttl=1)
    lan._handle(announcement, ("192.0.2.9", 9))
    assert "192.0.2.9:8790" in table.load()


@pytest.mark.parametrize("http_host", ["169.254.169.254/latest/meta-data#", "evil.example", "224.0.0.1", "0.0.0.0", "127.0.0.1:1@x"])
def test_handle_rejects_an_http_host_that_is_not_a_usable_ip(tmp_path, http_host):
    table = PeerTable(tmp_path / "peers.json")
    server = PresenceServer(8001, table, name="node-a", port=1, multicast_if="0.0.0.0", ttl=1)
    server._handle(json.dumps({"kind": "maestro-presence", "name": "b", "http_port": 8790, "http_host": http_host}).encode(), ("10.0.0.9", 9))
    assert table.load() == {}


def test_handle_formats_an_ipv6_http_host(tmp_path):
    table = PeerTable(tmp_path / "peers.json")
    server = PresenceServer(8001, table, name="node-a", port=1, multicast_if="0.0.0.0", ttl=1)
    server._handle(json.dumps({"kind": "maestro-presence", "name": "b", "http_port": 8790, "http_host": "fd00::5"}).encode(), ("10.0.0.9", 9))
    assert table.load()["10.0.0.9:8790"]["url"] == "http://[fd00::5]:8790"


@pytest.mark.parametrize("http_port", [True, 0, -1, 70000, "8790"])
def test_handle_rejects_an_invalid_http_port(tmp_path, http_port):
    table = PeerTable(tmp_path / "peers.json")
    server = PresenceServer(8001, table, name="node-a", port=1, multicast_if="0.0.0.0", ttl=1)
    server._handle(json.dumps({"kind": "maestro-presence", "name": "b", "http_port": http_port}).encode(), ("10.0.0.9", 9))
    assert table.load() == {}


def test_handle_strips_control_characters_from_names(tmp_path):
    table = PeerTable(tmp_path / "peers.json")
    server = PresenceServer(8001, table, name="node-a", port=1, multicast_if="0.0.0.0", ttl=1)
    evil = "\x1b]0;owned\x07\x1b[31mnode\u009b2J\r\n" + "x" * 200
    server._handle(json.dumps({"kind": "maestro-presence", "name": evil, "http_port": 8790}).encode(), ("10.0.0.9", 9))
    name = table.load()["10.0.0.9:8790"]["name"]
    assert name.startswith("]0;owned[31mnode2J") and len(name) == 64
    assert not any(ord(ch) < 32 or 127 <= ord(ch) < 160 for ch in name)
    # A name that is empty after cleaning, or not a string, falls back to the address.
    server._handle(json.dumps({"kind": "maestro-presence", "name": "\x1b\x07", "http_port": 8791}).encode(), ("10.0.0.9", 9))
    server._handle(json.dumps({"kind": "maestro-presence", "name": ["x"], "http_port": 8792}).encode(), ("10.0.0.9", 9))
    peers = table.load()
    assert peers["10.0.0.9:8791"]["name"] == "node-10.0.0.9:8791"
    assert peers["10.0.0.9:8792"]["name"] == "node-10.0.0.9:8792"


def test_peer_table_is_capped_and_drops_the_oldest_discovered_peers(tmp_path, monkeypatch):
    import maestro.discovery as disc

    table = PeerTable(tmp_path / "peers.json")
    table.add_static("lab", "http://10.0.0.9:8790")
    clock = [1000.0]
    monkeypatch.setattr(disc.time, "time", lambda: clock[0])
    for i in range(disc.MAX_PEERS + 20):
        clock[0] += 1
        table.upsert({"key": f"10.1.{i // 250}.{i % 250}:8790", "name": f"n{i}", "port": 8790, "address": "10.1.0.1"})
    peers = table.load()
    assert len(peers) == disc.MAX_PEERS
    assert "static:lab" in peers  # a manually added peer is never evicted
    assert "10.1.0.0:8790" not in peers  # the oldest discovered peers went first
    last = disc.MAX_PEERS + 19
    assert f"10.1.{last // 250}.{last % 250}:8790" in peers


def test_peer_table_prunes_peers_unseen_for_an_hour(tmp_path, monkeypatch):
    import maestro.discovery as disc

    path = tmp_path / "peers.json"
    now = time.time()
    path.write_text(json.dumps({
        "10.0.0.1:8790": {"name": "old", "port": 8790, "last_seen": now - disc.PRUNE_AFTER_S - 1},
        "static:lab": {"name": "lab", "url": "static:http://10.0.0.9:8790", "manual": True, "last_seen": now - 10 * disc.PRUNE_AFTER_S},
    }), encoding="utf-8")
    table = PeerTable(path)
    table.upsert({"key": "10.0.0.2:8790", "name": "new", "port": 8790, "address": "10.0.0.2"})
    assert set(table.load()) == {"static:lab", "10.0.0.2:8790"}


def test_peer_table_writes_only_when_something_changed(tmp_path, monkeypatch):
    import maestro.discovery as disc

    table = PeerTable(tmp_path / "peers.json")
    writes = []
    real_save = table.save
    monkeypatch.setattr(table, "save", lambda peers: writes.append(1) or real_save(peers))
    clock = [1000.0]
    monkeypatch.setattr(disc.time, "time", lambda: clock[0])
    peer = {"key": "10.0.0.2:8790", "name": "b", "port": 8790, "address": "10.0.0.2", "url": "http://10.0.0.2:8790"}
    table.upsert(dict(peer))
    clock[0] += 1
    table.upsert(dict(peer))  # the same announcement a second later: nothing to write
    assert len(writes) == 1
    clock[0] += disc.LAST_SEEN_REFRESH_S
    table.upsert(dict(peer))  # the stored last_seen is getting old: refresh it
    assert len(writes) == 2
    table.upsert({**peer, "name": "renamed"})  # a real change is written at once
    assert len(writes) == 3 and table.load()["10.0.0.2:8790"]["name"] == "renamed"


def test_peer_table_load_cleans_entries_written_by_older_versions(tmp_path):
    path = tmp_path / "peers.json"
    path.write_text(json.dumps({
        "10.0.0.1:8790": {"name": "\x1b[2Jevil\x07", "url": "http://10.0.0.1:8790\x1b[0m", "port": 8790, "last_seen": 1.0},
        "junk": "not a peer",
    }), encoding="utf-8")
    peers = PeerTable(path).load()
    assert peers == {"10.0.0.1:8790": {"name": "[2Jevil", "url": "http://10.0.0.1:8790[0m", "port": 8790, "last_seen": 1.0}}


def test_peers_cli_list_prints_no_control_characters(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    home.mkdir()
    (home / "peers.json").write_text(json.dumps({
        "10.0.0.1:8790": {"name": "\x1b]0;owned\x07evil", "url": "http://10.0.0.1:8790", "port": 8790, "last_seen": time.time()},
    }), encoding="utf-8")
    assert _peers_cli(tmp_path, monkeypatch, "peers", "list") == 0
    out = capsys.readouterr().out
    assert "]0;ownedevil" in out and "\x1b" not in out and "\x07" not in out


def test_discovery_is_off_for_a_loopback_daemon_unless_switched_on(monkeypatch):
    monkeypatch.delenv("MAESTRO_DISCOVERY", raising=False)
    assert discovery_enabled(loopback_only=True) is False
    assert discovery_enabled(loopback_only=False) is True
    for value in ("1", "true", "yes", "on"):
        monkeypatch.setenv("MAESTRO_DISCOVERY", value)
        assert discovery_enabled(loopback_only=True) is True
    monkeypatch.setenv("MAESTRO_DISCOVERY", "0")
    assert discovery_enabled(loopback_only=False) is False


def test_loopback_daemon_does_not_announce_by_default(tmp_path, monkeypatch):
    from maestro.daemon import MaestroDaemon

    home = tmp_path / "home"
    monkeypatch.setenv("MAESTRO_HOME", str(home))
    monkeypatch.delenv("MAESTRO_DISCOVERY", raising=False)
    monkeypatch.setenv("MAESTRO_DISCOVERY_PORT", str(_ephemeral_udp_port()))
    d = MaestroDaemon(state_dir=home, start_http=True, port=0, max_retries=0, backoff_s=0)
    try:
        assert d._presence is None
    finally:
        d.stop()


def test_peer_table_load_ignores_a_file_that_is_not_an_object(tmp_path):
    path = tmp_path / "peers.json"
    path.write_text("[1, 2]", encoding="utf-8")
    assert PeerTable(path).load() == {}


# ------------------------------------------------ review follow-ups (PR #31)
@pytest.mark.parametrize("http_host", ["127.0.0.1", "127.8.9.10", "::1", "169.254.169.254", "fe80::1", "::ffff:127.0.0.1", "::ffff:169.254.169.254"])
def test_handle_rejects_loopback_and_link_local_hosts_from_another_host(tmp_path, http_host):
    """Reviewer's r3b.py: a LAN host must not point peers at this machine's loopback or a metadata address."""
    table = PeerTable(tmp_path / "peers.json")
    server = PresenceServer(8001, table, name="node-a", port=1, multicast_if="0.0.0.0", ttl=1)
    server._handle(json.dumps({"kind": "maestro-presence", "name": "x", "http_port": 8790, "http_host": http_host}).encode(), ("192.168.1.50", 9786))
    assert table.load() == {}


def test_handle_accepts_a_loopback_host_from_a_loopback_sender(tmp_path):
    table = PeerTable(tmp_path / "peers.json")
    server = PresenceServer(8001, table, name="node-a", port=1, multicast_if="0.0.0.0", ttl=1)
    server._handle(json.dumps({"kind": "maestro-presence", "name": "x", "http_port": 8790, "http_host": "127.0.0.1"}).encode(), ("127.0.0.1", 9786))
    server._handle(json.dumps({"kind": "maestro-presence", "name": "y", "http_port": 8791, "http_host": "10.9.9.9"}).encode(), ("192.168.1.50", 9786))
    assert {key: peer["url"] for key, peer in table.load().items()} == {
        "127.0.0.1:8790": "http://127.0.0.1:8790",
        "192.168.1.50:8791": "http://10.9.9.9:8791",
    }


def _one_announce_tick(server: PresenceServer) -> _FakePresenceSocket:
    sock = _FakePresenceSocket(recv_results=[socket.timeout()])  # one quiet tick, then the socket "closes"
    server._sock = server._send_sock = sock
    server._loop()
    return sock


def test_loopback_host_is_not_announced_on_a_network_interface(tmp_path):
    """A daemon reachable only on 127.0.0.1 must not announce that address to the LAN."""
    server = PresenceServer(8001, PeerTable(tmp_path / "peers.json"), port=1, multicast_if="0.0.0.0", http_host="127.0.0.1")
    assert server.announces is False
    assert _one_announce_tick(server).sent == []


def test_loopback_host_is_announced_on_the_loopback_interface(tmp_path):
    server = PresenceServer(8001, PeerTable(tmp_path / "peers.json"), port=1, multicast_if="127.0.0.1", http_host="127.0.0.1")
    assert server.announces is True
    sent = _one_announce_tick(server).sent
    assert len(sent) == 1 and json.loads(sent[0][0])["http_host"] == "127.0.0.1"


def test_reachable_host_is_announced_on_a_network_interface(tmp_path):
    server = PresenceServer(8001, PeerTable(tmp_path / "peers.json"), port=1, multicast_if="0.0.0.0", http_host="192.0.2.44")
    assert server.announces is True
    assert len(_one_announce_tick(server).sent) == 1


def test_loopback_daemon_with_discovery_on_listens_but_does_not_announce(tmp_path, monkeypatch):
    """MAESTRO_DISCOVERY=1 on a loopback daemon with the default interface: peers are heard, 127.0.0.1 is never sent."""
    from maestro.daemon import MaestroDaemon

    home = tmp_path / "home"
    monkeypatch.setenv("MAESTRO_HOME", str(home))
    monkeypatch.setenv("MAESTRO_DISCOVERY", "1")
    monkeypatch.setenv("MAESTRO_DISCOVERY_IF", "0.0.0.0")
    monkeypatch.setenv("MAESTRO_DISCOVERY_PORT", str(_ephemeral_udp_port()))
    d = MaestroDaemon(state_dir=home, start_http=True, port=0, max_retries=0, backoff_s=0)
    try:
        assert d._presence is not None and d._presence.announces is False
    finally:
        d.stop()


def test_a_host_name_that_is_not_an_address_is_still_announced(tmp_path):
    """Only addresses are judged here; receivers drop a host that is not an IP address on their own."""
    server = PresenceServer(8001, PeerTable(tmp_path / "peers.json"), port=1, multicast_if="0.0.0.0", http_host="maestro.local")
    assert server.announces is True


# ------------------------------------------------ review follow-ups (PR #33)
def test_link_local_host_is_accepted_from_that_same_address(tmp_path):
    """On a link-local-only network (a Thunderbolt bridge) a peer announces its own 169.254.x.y address."""
    table = PeerTable(tmp_path / "peers.json")
    server = PresenceServer(8001, table, name="node-a", port=1, multicast_if="0.0.0.0", ttl=1)
    announce = lambda host: json.dumps({"kind": "maestro-presence", "name": "tb", "http_port": 8790, "http_host": host}).encode()  # noqa: E731
    server._handle(announce("169.254.10.20"), ("169.254.10.20", 9786))
    server._handle(announce("169.254.169.254"), ("169.254.10.21", 9786))  # another address: still rejected
    assert {key: peer["url"] for key, peer in table.load().items()} == {"169.254.10.20:8790": "http://169.254.10.20:8790"}


def test_link_local_sender_without_http_host_is_accepted(tmp_path):
    table = PeerTable(tmp_path / "peers.json")
    server = PresenceServer(8001, table, name="node-a", port=1, multicast_if="0.0.0.0", ttl=1)
    server._handle(json.dumps({"kind": "maestro-presence", "name": "tb", "http_port": 8790}).encode(), ("169.254.10.10", 9786))
    assert table.load()["169.254.10.10:8790"]["url"] == "http://169.254.10.10:8790"


def test_a_daemon_bound_to_a_link_local_address_announces_itself(tmp_path):
    server = PresenceServer(8001, PeerTable(tmp_path / "peers.json"), port=1, multicast_if="0.0.0.0", http_host="169.254.10.20")
    assert server.announces is True
    assert len(_one_announce_tick(server).sent) == 1


@pytest.mark.parametrize("host", ["169.254.169.254", "169.254.170.2", "fd00:ec2::254"])
def test_cloud_metadata_addresses_are_rejected_even_from_their_own_address(tmp_path, host):
    """A UDP source address is easy to fake, so the well-known metadata endpoints are never recorded."""
    table = PeerTable(tmp_path / "peers.json")
    server = PresenceServer(8001, table, name="node-a", port=1, multicast_if="0.0.0.0", ttl=1)
    server._handle(json.dumps({"kind": "maestro-presence", "name": "md", "http_port": 80, "http_host": host}).encode(), (host, 9786))
    server._handle(json.dumps({"kind": "maestro-presence", "name": "md", "http_port": 81}).encode(), (host, 9786))
    server._handle(json.dumps({"kind": "maestro-presence", "name": "md", "http_port": 82, "http_host": host}).encode(), ("127.0.0.1", 9786))
    assert table.load() == {}
