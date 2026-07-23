import importlib.machinery
import importlib.util
import json
from pathlib import Path

loader = importlib.machinery.SourceFileLoader(
    "clouddevbox", str(Path(__file__).resolve().parent.parent / "clouddevbox"))
spec = importlib.util.spec_from_loader("clouddevbox", loader)
cdb = importlib.util.module_from_spec(spec)
loader.exec_module(cdb)


def test_name_re():
    ok = ["tst1", "alpha", "my-box-2", "ab"]
    bad = ["a", "-abc", "abc-", "Abc", "a" * 30, "a_b", ""]
    for n in ok:
        assert cdb.NAME_RE.match(n), n
    for n in bad:
        assert not cdb.NAME_RE.match(n), n


def test_autostop_re():
    for v in ["6h", "10h", "90m", "1h", "off"]:
        assert cdb.AUTOSTOP_RE.match(v), v
    for v in ["6", "h", "6H", "off2", "-1h", "1d", ""]:
        assert not cdb.AUTOSTOP_RE.match(v), v


def test_key_re():
    assert cdb.KEY_RE.match("hskey-auth-v-" + "a" * 40)  # v0.28 urlsafe-b64 shape
    assert cdb.KEY_RE.match("hskey-auth-v-aB3_-" + "x" * 30)
    assert cdb.KEY_RE.match("0123456789abcdef0123456789abcdef")  # legacy hex
    assert not cdb.KEY_RE.match("short")
    assert not cdb.KEY_RE.match("has space " + "a" * 30)


CAMEL = json.dumps([
    {"id": 7, "name": "devbox-tst1", "givenName": "devbox-tst1",
     "ipAddresses": ["100.64.0.24", "fd7a:115c:a1e0::1"],
     "forcedTags": ["tag:devbox"], "online": True,
     "lastSeen": "2026-07-22T10:00:00Z"},
    {"id": 3, "name": "vault", "givenName": "vault",
     "ipAddresses": ["100.64.0.3"], "forcedTags": ["tag:svc"], "online": True},
])

SNAKE = json.dumps([
    {"id": "7", "name": "devbox-tst1", "given_name": "devbox-tst1",
     "ip_addresses": ["100.64.0.24"], "forced_tags": ["tag:devbox"],
     "online": False, "last_seen": "2026-07-22T10:00:00Z"},
])


def test_parse_nodes_camel():
    nodes = cdb.parse_nodes(CAMEL)
    assert set(nodes) == {"devbox-tst1"}
    n = nodes["devbox-tst1"]
    assert n["id"] == "7" and n["online"] is True and n["ip"] == "100.64.0.24"


def test_parse_nodes_snake():
    nodes = cdb.parse_nodes(SNAKE)
    n = nodes["devbox-tst1"]
    assert n["id"] == "7" and n["online"] is False and n["ip"] == "100.64.0.24"


def test_parse_nodes_wrapper_dict():
    wrapped = json.dumps({"nodes": json.loads(CAMEL)})
    assert set(cdb.parse_nodes(wrapped)) == {"devbox-tst1"}


# Real headscale v0.28 shape (captured live 2026-07-22): snake_case,
# preauthkey tags under plain "tags", last_seen as protobuf {seconds,nanos}.
V028 = json.dumps([
    {"id": 37, "ip_addresses": ["100.64.0.31", "fd7a:115c:a1e0::1f"],
     "name": "devbox-tst1", "user": {"id": 2147455555, "name": "tagged-devices"},
     "last_seen": {"seconds": 1784733548, "nanos": 861724657},
     "given_name": "devbox-tst1", "online": True, "tags": ["tag:devbox"],
     "pre_auth_key": {"acl_tags": ["tag:devbox"], "user": {"id": 2}}},
    {"id": 1, "name": "infra-vps", "given_name": "infra-vps",
     "ip_addresses": ["100.64.0.1"], "online": True, "tags": ["tag:infra"]},
])


def test_parse_nodes_v028_real_shape():
    nodes = cdb.parse_nodes(V028)
    assert set(nodes) == {"devbox-tst1"}
    n = nodes["devbox-tst1"]
    assert n["id"] == "37" and n["online"] is True
    assert n["ip"] == "100.64.0.31"
    assert n["last_seen"] == 1784733548


class _Args:
    ssm = False
    show = False
    name = "x"
    profile = "p"
    command = []


def _ssh_path(reachable):
    """Run cmd_ssh with stubbed probes; return the ssh argv it chose."""
    saved = (cdb.hs_nodes, cdb._tcp_reachable, cdb.subprocess.run)
    ran = []
    try:
        cdb.hs_nodes = lambda: {
            "devbox-x": {"id": "1", "online": True, "ip": "100.64.0.99",
                         "last_seen": None}}
        cdb._tcp_reachable = lambda h, p, t: reachable.get((h, p), False)
        cdb.subprocess.run = lambda cmd, **kw: type("R", (), {"returncode": 0})()
        cdb.subprocess.run = (lambda cmd, **kw:
                              (ran.append(cmd), type("R", (), {"returncode": 0})())[1])
        try:
            cdb.cmd_ssh(_Args(), None, None)
        except SystemExit as e:
            assert e.code == 0
        return ran[0]
    finally:
        cdb.hs_nodes, cdb._tcp_reachable, cdb.subprocess.run = saved


def test_ssh_prefers_proxy_when_up():
    cmd = _ssh_path({(cdb.SOCKS_HOST, cdb.SOCKS_PORT): True})
    assert any("ProxyCommand" in a for a in cmd)
    assert cmd[-1] == "100.64.0.99"


def test_ssh_falls_back_to_direct_route():
    cmd = _ssh_path({("100.64.0.99", 22): True})
    assert not any("ProxyCommand" in a for a in cmd)
    assert cmd[-1] == "100.64.0.99"


def test_ssh_errors_when_no_path():
    import pytest
    saved = (cdb.hs_nodes, cdb._tcp_reachable)
    try:
        cdb.hs_nodes = lambda: {
            "devbox-x": {"id": "1", "online": True, "ip": "100.64.0.99",
                         "last_seen": None}}
        cdb._tcp_reachable = lambda h, p, t: False
        with pytest.raises(cdb.CliError, match="no path"):
            cdb.cmd_ssh(_Args(), None, None)
    finally:
        cdb.hs_nodes, cdb._tcp_reachable = saved
