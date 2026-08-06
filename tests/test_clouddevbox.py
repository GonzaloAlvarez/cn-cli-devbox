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
    saved = (cdb.hs_nodes, cdb._tcp_reachable, cdb.subprocess.run,
             cdb._SSH_KEY_CACHE)
    ran = []
    try:
        cdb._SSH_KEY_CACHE = "/fake/key.pem"
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
        (cdb.hs_nodes, cdb._tcp_reachable, cdb.subprocess.run,
         cdb._SSH_KEY_CACHE) = saved


def test_ssh_prefers_proxy_when_up():
    cmd = _ssh_path({(cdb.SOCKS_HOST, cdb.SOCKS_PORT): True})
    assert any("ProxyCommand" in a for a in cmd)
    assert cmd[-1] == "100.64.0.99"


def test_ssh_falls_back_to_direct_route():
    cmd = _ssh_path({("100.64.0.99", 22): True})
    assert not any("ProxyCommand" in a for a in cmd)
    assert cmd[-1] == "100.64.0.99"


def test_new_stale_node_suggests_destroy(monkeypatch):
    """The stale-node hint must go through `clouddevbox destroy` (the CLI's
    authenticated ssh path), never a raw `ssh hs.gn.al` one-liner - that
    fails silently on machines whose local username differs from VPS_USER."""
    import pytest

    class Args:
        name = "dev1"
        profile = "p"
        type = "m7g.large"
        disk = 50
        autostop = "6h"
        plugins = "kauket"

    class Session:
        def client(self, name):
            return None

    monkeypatch.setattr(cdb, "find_instance", lambda ec2, name, states=None: None)
    monkeypatch.setattr(cdb, "stack_status", lambda cfn, name: (None, None, None))
    monkeypatch.setattr(cdb, "hs_nodes", lambda: {
        "devbox-dev1": {"id": "37", "online": False, "ip": None, "last_seen": None}})
    with pytest.raises(cdb.CliError) as e:
        cdb.cmd_new(Args(), Session(), "123456789012")
    msg = str(e.value)
    assert "clouddevbox destroy dev1 --profile p" in msg
    assert "id 37" in msg
    assert "ssh " not in msg


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


# ---------------------------------------------------------------------------
# ssh key resolution: email-tagged ~/.ssh candidates -> probe -> kauket ssh.*
# ---------------------------------------------------------------------------
import subprocess


def _reset_key_state(monkeypatch):
    monkeypatch.setattr(cdb, "_SSH_KEY_CACHE", None)
    monkeypatch.setattr(cdb, "_SSH_KEY_TMPDIR", None)
    monkeypatch.delenv("CLOUDDEVBOX_SSH_KEY", raising=False)
    monkeypatch.delenv("KAUKET_HOME", raising=False)


def _gen_key(dirpath, name, comment):
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", comment,
                    "-f", str(dirpath / name)], check=True)


def test_candidate_keys_discovery(tmp_path, monkeypatch):
    _reset_key_state(monkeypatch)
    monkeypatch.setattr(cdb, "SSH_DIR", str(tmp_path))
    _gen_key(tmp_path, "mine", cdb.EMAIL)                 # .pub comment matches
    _gen_key(tmp_path, "work", "someone@corp.example")    # .pub comment does not
    _gen_key(tmp_path, "embedded", cdb.EMAIL)             # no .pub, embedded comment
    (tmp_path / "embedded.pub").unlink()
    (tmp_path / "legacy.pem").write_text(
        "-----BEGIN RSA PRIVATE KEY-----\nnotakey\n-----END RSA PRIVATE KEY-----\n")
    (tmp_path / "known_hosts").write_text("x\n")
    assert cdb._candidate_keys() == [str(tmp_path / "embedded"), str(tmp_path / "mine")]


def test_ssh_key_env_override(tmp_path, monkeypatch):
    _reset_key_state(monkeypatch)
    key = tmp_path / "mykey.pem"
    key.write_text("k")
    monkeypatch.setenv("CLOUDDEVBOX_SSH_KEY", str(key))
    assert cdb.ssh_key() == str(key)


def test_ssh_key_env_override_missing(tmp_path, monkeypatch):
    import pytest
    _reset_key_state(monkeypatch)
    monkeypatch.setenv("CLOUDDEVBOX_SSH_KEY", str(tmp_path / "nope.pem"))
    with pytest.raises(cdb.CliError, match="not a file"):
        cdb.ssh_key()


def test_ssh_key_first_working_local_candidate(monkeypatch):
    _reset_key_state(monkeypatch)
    monkeypatch.setattr(cdb, "_candidate_keys", lambda: ["/k1", "/k2", "/k3"])
    monkeypatch.setattr(cdb, "_key_works", lambda p: p == "/k2")
    assert cdb.ssh_key() == "/k2"
    monkeypatch.setattr(cdb, "_key_works", lambda p: False)  # cached: not re-probed
    assert cdb.ssh_key() == "/k2"


def _fake_kauket(tmp_path, body):
    exe = tmp_path / "kauket"
    exe.write_text("#!/bin/sh\n" + body + "\n")
    exe.chmod(0o755)
    home = tmp_path / "kauket-home"
    home.mkdir(exist_ok=True)
    return str(exe), str(home)


def _use_kauket(tmp_path, monkeypatch, body):
    _reset_key_state(monkeypatch)
    exe, home = _fake_kauket(tmp_path, body)
    monkeypatch.setattr(cdb, "SSH_DIR", str(tmp_path / "empty-ssh"))
    monkeypatch.setattr(cdb, "KAUKET_CLIENT_HOME", home)
    monkeypatch.setattr(cdb, "WORKDIR", tmp_path / "wd")
    monkeypatch.setattr(cdb.shutil, "which",
                        lambda n: exe if n == "kauket" else None)


def test_ssh_key_kauket_fallback_tmp_and_cleanup(tmp_path, monkeypatch):
    _use_kauket(tmp_path, monkeypatch,
                'if [ "$1" = "list" ]; then echo "ssh.main_key  profiles=ssh"; '
                'echo "vps.env"; exit 0; fi\n'
                'if [ "$1" = "get" ]; then printf "FAKEKEY"; exit 0; fi\nexit 1')
    monkeypatch.setattr(cdb, "_key_works", lambda p: True)
    path = Path(cdb.ssh_key())
    assert path.name == "ssh.main_key" and path.read_bytes() == b"FAKEKEY"
    assert (path.stat().st_mode & 0o777) == 0o600
    assert (path.parent.stat().st_mode & 0o777) == 0o700
    cdb._cleanup()
    assert not path.exists() and not path.parent.exists()


def test_ssh_key_kauket_get_sync_retry(tmp_path, monkeypatch):
    _use_kauket(tmp_path, monkeypatch,
                'if [ "$1" = "list" ]; then echo "ssh.main_key"; exit 0; fi\n'
                'for a in "$@"; do if [ "$a" = "--no-sync" ]; then '
                'echo stale >&2; exit 5; fi; done\nprintf "SYNCEDKEY"')
    monkeypatch.setattr(cdb, "_key_works", lambda p: True)
    assert Path(cdb.ssh_key()).read_bytes() == b"SYNCEDKEY"
    cdb._cleanup()


def test_ssh_key_kauket_not_granted_hint(tmp_path, monkeypatch):
    import pytest
    _use_kauket(tmp_path, monkeypatch,
                'if [ "$1" = "list" ]; then exit 0; fi\nexit 5')
    monkeypatch.setattr(cdb, "_key_works", lambda p: True)
    with pytest.raises(cdb.CliError, match="enroll"):
        cdb.ssh_key()


def test_ssh_key_no_kauket_on_path(tmp_path, monkeypatch):
    import pytest
    _reset_key_state(monkeypatch)
    monkeypatch.setattr(cdb, "SSH_DIR", str(tmp_path / "empty-ssh"))
    monkeypatch.setattr(cdb.shutil, "which", lambda n: None)
    with pytest.raises(cdb.CliError, match="kauket is not on PATH"):
        cdb.ssh_key()


# ---------------------------------------------------------------------------
# --profile: optional everywhere; interactive bullet picker when omitted
# ---------------------------------------------------------------------------
import io
import sys as _sys
import types


class _Tty(io.StringIO):
    def isatty(self):
        return True


def test_profile_optional_in_parser():
    args = cdb.build_parser().parse_args(["list"])
    assert args.profile is None
    args = cdb.build_parser().parse_args(["list", "--profile", "p"])
    assert args.profile == "p"


def test_select_profile_requires_tty(monkeypatch):
    import pytest
    monkeypatch.setattr(cdb.sys, "stdin", io.StringIO())  # isatty() -> False
    with pytest.raises(cdb.CliError, match="--profile is required"):
        cdb.select_profile()


def test_select_profile_no_profiles(monkeypatch):
    import pytest
    monkeypatch.setattr(cdb.sys, "stdin", _Tty())
    monkeypatch.setattr(cdb, "_available_profiles", lambda: [])
    with pytest.raises(cdb.CliError, match="no AWS profiles"):
        cdb.select_profile()


def test_select_profile_launches_bullet(monkeypatch):
    monkeypatch.setattr(cdb.sys, "stdin", _Tty())
    monkeypatch.setattr(cdb, "_available_profiles", lambda: ["personal", "work"])
    captured = {}

    class FakeBullet:
        def __init__(self, **kw):
            captured.update(kw)

        def launch(self):
            return "work"

    monkeypatch.setitem(_sys.modules, "bullet",
                        types.SimpleNamespace(Bullet=FakeBullet))
    assert cdb.select_profile() == "work"
    assert captured["choices"] == ["personal", "work"]
