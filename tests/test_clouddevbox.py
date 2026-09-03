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
        kvm = False
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
# copy / tun (v1.5.0): shared tailnet route, scp argv, port validation
# ---------------------------------------------------------------------------
class _CopyArgs:
    show = False
    profile = "p"

    def __init__(self, src, dst):
        self.src, self.dst = src, dst


class _TunArgs:
    show = False
    profile = "p"
    name = "xb"

    def __init__(self, local_port, remote_port):
        self.local_port, self.remote_port = local_port, remote_port


def _net_argv(fn, args, reachable):
    """Run a connectivity command with stubbed probes; return its argv."""
    saved = (cdb.hs_nodes, cdb._tcp_reachable, cdb.subprocess.run,
             cdb._SSH_KEY_CACHE)
    ran = []
    try:
        cdb._SSH_KEY_CACHE = "/fake/key.pem"
        cdb.hs_nodes = lambda: {
            "devbox-xb": {"id": "1", "online": True, "ip": "100.64.0.99",
                         "last_seen": None}}
        cdb._tcp_reachable = lambda h, p, t: reachable.get((h, p), False)
        cdb.subprocess.run = (lambda cmd, **kw:
                              (ran.append(cmd), type("R", (), {"returncode": 0})())[1])
        try:
            fn(args, None, None)
        except SystemExit as e:
            assert e.code == 0
        return ran[0]
    finally:
        (cdb.hs_nodes, cdb._tcp_reachable, cdb.subprocess.run,
         cdb._SSH_KEY_CACHE) = saved


def test_split_remote():
    assert cdb._split_remote("xb:/tmp/f") == ("xb", "/tmp/f")
    assert cdb._split_remote("alpha:") == ("alpha", "")
    assert cdb._split_remote("my-box-2:rel/path") == ("my-box-2", "rel/path")
    for local in ("./a:b", "/abs:p", "plain", "Abc:/x", "a:/x"):
        assert cdb._split_remote(local) is None, local


def test_copy_local_to_remote_uses_proxy():
    cmd = _net_argv(cdb.cmd_copy, _CopyArgs("report.txt", "xb:/tmp/f"),
                    {(cdb.SOCKS_HOST, cdb.SOCKS_PORT): True})
    assert cmd[0] == "scp"
    assert any("ProxyCommand" in a for a in cmd)
    assert "-l" not in cmd
    assert cmd[-2:] == ["report.txt", f"{cdb.VPS_USER}@100.64.0.99:/tmp/f"]


def test_copy_remote_to_local_direct_route():
    cmd = _net_argv(cdb.cmd_copy, _CopyArgs("xb:/var/log/syslog", "out.log"),
                    {("100.64.0.99", 22): True})
    assert cmd[0] == "scp"
    assert not any("ProxyCommand" in a for a in cmd)
    assert cmd[-2:] == [f"{cdb.VPS_USER}@100.64.0.99:/var/log/syslog", "out.log"]


def test_copy_requires_exactly_one_remote():
    import pytest
    for src, dst in (("a.txt", "b.txt"), ("xb:/a", "xb:/b")):
        with pytest.raises(cdb.CliError, match="exactly one"):
            cdb.cmd_copy(_CopyArgs(src, dst), None, None)


def test_tun_argv_and_banner(capsys):
    cmd = _net_argv(cdb.cmd_tun, _TunArgs(8080, 8000),
                    {(cdb.SOCKS_HOST, cdb.SOCKS_PORT): True})
    assert cmd[0] == "ssh" and "-N" in cmd
    assert cmd[cmd.index("-L") + 1] == "8080:127.0.0.1:8000"
    assert cmd[cmd.index("-l") + 1] == cdb.VPS_USER
    assert cmd[-1] == "100.64.0.99"
    assert "tunnel open: localhost:8080 -> devbox-xb:8000" in capsys.readouterr().out


def test_tun_port_validation():
    import pytest
    for lp, rp in ((0, 80), (8080, 70000)):
        with pytest.raises(cdb.CliError, match="invalid .* port"):
            cdb.cmd_tun(_TunArgs(lp, rp), None, None)


def test_parser_copy_tun():
    args = cdb.build_parser().parse_args(["copy", "xb:/a", "b", "--profile", "p"])
    assert args.fn is cdb.cmd_copy and (args.src, args.dst) == ("xb:/a", "b")
    args = cdb.build_parser().parse_args(["tun", "xb", "8080", "80"])
    assert args.fn is cdb.cmd_tun
    assert (args.local_port, args.remote_port) == (8080, 80)


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


def test_candidate_keys_canonical_first(tmp_path, monkeypatch):
    _reset_key_state(monkeypatch)
    monkeypatch.setattr(cdb, "SSH_DIR", str(tmp_path))
    _gen_key(tmp_path, "mine", cdb.EMAIL)
    _gen_key(tmp_path, "tmpname", "whatever-comment")     # kauket-style: no .pub, no email
    (tmp_path / "tmpname.pub").unlink()
    (tmp_path / "tmpname").rename(tmp_path / "gonzalo_main_private_key")
    assert cdb._candidate_keys() == [str(tmp_path / "gonzalo_main_private_key"),
                                     str(tmp_path / "mine")]


def test_candidate_keys_canonical_pem_and_dedupe(tmp_path, monkeypatch):
    _reset_key_state(monkeypatch)
    monkeypatch.setattr(cdb, "SSH_DIR", str(tmp_path))
    _gen_key(tmp_path, "k1", "no-pub-no-comment-match")
    (tmp_path / "k1.pub").unlink()
    (tmp_path / "k1").rename(tmp_path / "gonzalo_main_private_key")
    _gen_key(tmp_path, "k2", cdb.EMAIL)                   # .pem variant WITH email .pub
    (tmp_path / "k2").rename(tmp_path / "gonzalo_main_private_key.pem")
    (tmp_path / "k2.pub").rename(tmp_path / "gonzalo_main_private_key.pem.pub")
    got = cdb._candidate_keys()
    assert got == [str(tmp_path / "gonzalo_main_private_key"),
                   str(tmp_path / "gonzalo_main_private_key.pem")]


def test_kauket_client_home_order(tmp_path, monkeypatch):
    _reset_key_state(monkeypatch)
    exe = tmp_path / "kauket"
    exe.write_text("#!/bin/sh\nexit 0\n")
    exe.chmod(0o755)
    monkeypatch.setattr(cdb.shutil, "which",
                        lambda n: str(exe) if n == "kauket" else None)
    default = tmp_path / "default-home"
    legacy = tmp_path / "legacy-home"
    monkeypatch.setattr(cdb, "_default_kauket_homes",
                        lambda: [str(default), str(legacy)])
    envhome = tmp_path / "env-home"
    envhome.mkdir()
    monkeypatch.setenv("KAUKET_HOME", str(envhome))
    assert cdb._kauket_client()[1]["KAUKET_HOME"] == str(envhome)
    monkeypatch.delenv("KAUKET_HOME")
    kauket, why = cdb._kauket_client()
    assert kauket is None and "no kauket client home" in why
    legacy.mkdir()
    assert cdb._kauket_client()[1]["KAUKET_HOME"] == str(legacy)
    default.mkdir()
    assert cdb._kauket_client()[1]["KAUKET_HOME"] == str(default)


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
    monkeypatch.setattr(cdb, "_available_profiles", lambda: ["personal", "work"])
    with pytest.raises(cdb.CliError, match="--profile is required"):
        cdb.select_profile()


def test_select_profile_single_auto_selects(monkeypatch):
    # a sole configured profile is used without a picker, even without a tty
    monkeypatch.setattr(cdb.sys, "stdin", io.StringIO())  # isatty() -> False
    monkeypatch.setattr(cdb, "_available_profiles", lambda: ["personal"])
    assert cdb.select_profile() == "personal"


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


# ---------------------------------------------------------------------------
# kvm / nested virtualization (v1.6.0)
# ---------------------------------------------------------------------------
def test_type_re_metal_sizes():
    for t in ["c7i-flex.large", "i7i.metal-24xl", "m8id.2xlarge",
              "m7i.metal-48xl", "m7g.metal", "m7i.large"]:
        assert cdb.TYPE_RE.match(t), t
    for t in ["M7i.large", "m7i.", ".large", "m7i", ""]:
        assert not cdb.TYPE_RE.match(t), t


def test_kvm_supported_and_arch():
    for t in ["m7i.large", "c7i-flex.xlarge", "x8i.large", "m8id.2xlarge",
              "c7i.metal-24xl", "m7g.metal"]:
        assert cdb.kvm_supported(t), t
    for t in ["m7g.large", "m5.large", "t4g.small"]:
        assert not cdb.kvm_supported(t), t
    assert cdb.type_arch("m7g.large") == "arm64"
    assert cdb.type_arch("t4g.small") == "arm64"
    for t in ["i7i.xlarge", "c7i-flex.large", "m8id.large", "m7i.large"]:
        assert cdb.type_arch(t) == "x86_64", t


def _instance(itype="m7i.large", state="stopped", arch="x86_64",
              nested="disabled"):
    return {"InstanceId": "i-123", "InstanceType": itype,
            "State": {"Name": state}, "Architecture": arch,
            "CpuOptions": {"CoreCount": 1, "ThreadsPerCore": 2,
                           "NestedVirtualization": nested}}


def test_kvm_state():
    assert cdb.kvm_state(_instance(nested="enabled")) == "enabled"
    assert cdb.kvm_state(_instance()) == "disabled"
    assert cdb.kvm_state(_instance(itype="m7g.metal")) == "enabled (bare metal)"
    assert cdb.kvm_state({"InstanceType": "m5.large"}) == "disabled"  # no CpuOptions


class _FakeEc2:
    def __init__(self, instance):
        self.instance, self.calls = instance, []

    def describe_instances(self, **kw):
        self.calls.append(("describe_instances", kw))
        return {"Reservations": [{"Instances": [self.instance]}]}

    def modify_instance_attribute(self, **kw):
        self.calls.append(("modify_instance_attribute", kw))
        self.instance["InstanceType"] = kw["InstanceType"]["Value"]

    def modify_instance_cpu_options(self, **kw):
        self.calls.append(("modify_instance_cpu_options", kw))
        self.instance["CpuOptions"]["NestedVirtualization"] = \
            kw["NestedVirtualization"]

    def stop_instances(self, **kw):
        self.calls.append(("stop_instances", kw))

    def start_instances(self, **kw):
        self.calls.append(("start_instances", kw))

    def get_waiter(self, name):
        self.calls.append(("get_waiter", name))
        return type("W", (), {"wait": lambda s, **kw: None})()


class _FakeSession:
    def __init__(self, ec2):
        self._ec2 = ec2

    def client(self, name):
        return self._ec2


class _KvmArgs:
    profile = "p"
    name = "xb"
    type = None


def _kvm_setup(monkeypatch, instance):
    ec2 = _FakeEc2(instance)
    monkeypatch.setattr(cdb, "_require_instance",
                        lambda session, name, states=None: instance)
    monkeypatch.setattr(cdb, "_boto_errors",
                        lambda: (type("CE", (Exception,), {}),
                                 type("PE", (Exception,), {})))
    return ec2, _FakeSession(ec2)


def test_kvm_enable_requires_stopped(monkeypatch):
    import pytest
    _, session = _kvm_setup(monkeypatch, _instance(state="running"))
    with pytest.raises(cdb.CliError, match="clouddevbox stop xb --profile p"):
        cdb.cmd_kvm_enable(_KvmArgs(), session, "1")


def test_kvm_enable_arm_errors(monkeypatch):
    import pytest
    _, session = _kvm_setup(monkeypatch, _instance(itype="m7g.large", arch="arm64"))
    with pytest.raises(cdb.CliError, match="--kvm"):
        cdb.cmd_kvm_enable(_KvmArgs(), session, "1")


def test_kvm_enable_unsupported_family_hint(monkeypatch):
    import pytest
    _, session = _kvm_setup(monkeypatch, _instance(itype="m5.large"))
    with pytest.raises(cdb.CliError, match="--type m7i.large"):
        cdb.cmd_kvm_enable(_KvmArgs(), session, "1")


def test_kvm_enable(monkeypatch):
    ec2, session = _kvm_setup(monkeypatch, _instance())
    cdb.cmd_kvm_enable(_KvmArgs(), session, "1")
    mods = [c for c in ec2.calls if c[0] == "modify_instance_cpu_options"]
    assert len(mods) == 1
    assert mods[0][1] == {"InstanceId": "i-123", "CoreCount": 1,
                          "ThreadsPerCore": 2, "NestedVirtualization": "enabled"}


def test_kvm_enable_with_type_switch(monkeypatch):
    class Args(_KvmArgs):
        type = "m7i.large"

    ec2, session = _kvm_setup(monkeypatch, _instance(itype="m5.large"))
    cdb.cmd_kvm_enable(Args(), session, "1")
    ops = [c[0] for c in ec2.calls
           if c[0].startswith("modify")]
    assert ops == ["modify_instance_attribute", "modify_instance_cpu_options"]
    attr = next(c[1] for c in ec2.calls if c[0] == "modify_instance_attribute")
    assert attr["InstanceType"] == {"Value": "m7i.large"}


def test_kvm_enable_already_enabled(monkeypatch, capsys):
    ec2, session = _kvm_setup(monkeypatch, _instance(nested="enabled"))
    cdb.cmd_kvm_enable(_KvmArgs(), session, "1")
    assert "already" in capsys.readouterr().out
    assert not any(c[0].startswith("modify") for c in ec2.calls)


def test_kvm_enable_metal_noop(monkeypatch, capsys):
    ec2, session = _kvm_setup(monkeypatch,
                              _instance(itype="c7i.metal-24xl", state="running"))
    cdb.cmd_kvm_enable(_KvmArgs(), session, "1")
    assert "bare metal" in capsys.readouterr().out
    assert not ec2.calls


def test_kvm_disable(monkeypatch):
    ec2, session = _kvm_setup(monkeypatch, _instance(nested="enabled"))
    cdb.cmd_kvm_disable(_KvmArgs(), session, "1")
    mods = [c for c in ec2.calls if c[0] == "modify_instance_cpu_options"]
    assert mods[0][1]["NestedVirtualization"] == "disabled"


def test_new_kvm_default_type_and_context(monkeypatch):
    import pytest

    class Args:
        name = "kb1"
        profile = "p"
        type = None
        kvm = True
        disk = 50
        autostop = "6h"
        plugins = "kauket"

    class Boom(Exception):
        pass

    class Ssm:
        def put_parameter(self, **kw):
            pass

        def delete_parameter(self, **kw):
            pass

    class Session:
        def client(self, name):
            return Ssm()

    recorded = {}

    def fake_deploy(repo, profile, account, stacks, context):
        recorded.update(context)
        raise Boom()

    monkeypatch.setattr(cdb, "find_instance", lambda ec2, name, states=None: None)
    monkeypatch.setattr(cdb, "stack_status", lambda cfn, name: (None, None, None))
    monkeypatch.setattr(cdb, "hs_nodes", lambda: {})
    monkeypatch.setattr(cdb, "resolve_cdk_repo", lambda: "/tmp/fake-repo")
    monkeypatch.setattr(cdb, "hs_mint_key", lambda: "hskey-auth-v-" + "a" * 40)
    monkeypatch.setattr(cdb, "cdk_deploy", fake_deploy)
    a = Args()
    with pytest.raises(Boom):
        cdb.cmd_new(a, Session(), "123456789012")
    assert a.type == "m7i.large"
    assert recorded["type"] == "m7i.large" and recorded["kvm"] == "1"


def test_new_kvm_rejects_graviton_type():
    import pytest

    class Args:
        name = "kb1"
        profile = "p"
        type = "m7g.large"
        kvm = True

    with pytest.raises(cdb.CliError, match="m7i.large"):
        cdb.cmd_new(Args(), None, "123456789012")


def test_new_without_kvm_omits_context(monkeypatch):
    import pytest

    class Args:
        name = "kb1"
        profile = "p"
        type = None
        kvm = False
        disk = 50
        autostop = "6h"
        plugins = "kauket"

    class Boom(Exception):
        pass

    class Ssm:
        def put_parameter(self, **kw):
            pass

        def delete_parameter(self, **kw):
            pass

    class Session:
        def client(self, name):
            return Ssm()

    recorded = {}

    def fake_deploy(repo, profile, account, stacks, context):
        recorded.update(context)
        raise Boom()

    monkeypatch.setattr(cdb, "find_instance", lambda ec2, name, states=None: None)
    monkeypatch.setattr(cdb, "stack_status", lambda cfn, name: (None, None, None))
    monkeypatch.setattr(cdb, "hs_nodes", lambda: {})
    monkeypatch.setattr(cdb, "resolve_cdk_repo", lambda: "/tmp/fake-repo")
    monkeypatch.setattr(cdb, "hs_mint_key", lambda: "hskey-auth-v-" + "a" * 40)
    monkeypatch.setattr(cdb, "cdk_deploy", fake_deploy)
    a = Args()
    with pytest.raises(Boom):
        cdb.cmd_new(a, Session(), "123456789012")
    assert a.type == "m7g.large"
    assert "kvm" not in recorded


def test_ensure_kvm_heals(monkeypatch):
    ec2, session = _kvm_setup(monkeypatch, _instance())
    monkeypatch.setattr(cdb, "wait_tailnet_online",
                        lambda name, timeout=300: {"ip": "100.64.0.99"})
    cdb.ensure_kvm_enabled(session, "xb", "i-123")
    ops = [c[0] for c in ec2.calls if c[0] != "describe_instances"
           and c[0] != "get_waiter"]
    assert ops == ["stop_instances", "modify_instance_cpu_options",
                   "start_instances"]


def test_ensure_kvm_noop_when_enabled(monkeypatch, capsys):
    ec2, session = _kvm_setup(monkeypatch, _instance(nested="enabled"))
    cdb.ensure_kvm_enabled(session, "xb", "i-123")
    assert "confirmed enabled" in capsys.readouterr().out
    assert [c[0] for c in ec2.calls] == ["describe_instances"]


def test_status_kvm_line(monkeypatch, capsys):
    for inst, expect in ((_instance(nested="enabled"), "kvm:       enabled"),
                         (_instance(), "kvm:       disabled"),
                         (_instance(itype="m7g.metal"),
                          "kvm:       enabled (bare metal)")):
        monkeypatch.setattr(cdb, "_require_instance",
                            lambda session, name, states=None, i=inst: i)
        monkeypatch.setattr(cdb, "hs_nodes", lambda: None)
        cdb.cmd_status(_KvmArgs(), None, "1")
        assert expect in capsys.readouterr().out


def test_parser_kvm():
    args = cdb.build_parser().parse_args(
        ["kvm", "enable", "xb", "--type", "m7i.large", "--profile", "p"])
    assert args.fn is cdb.cmd_kvm_enable
    assert args.name == "xb" and args.type == "m7i.large"
    args = cdb.build_parser().parse_args(["kvm", "disable", "xb"])
    assert args.fn is cdb.cmd_kvm_disable
    args = cdb.build_parser().parse_args(["new", "kb1", "--kvm"])
    assert args.kvm is True and args.type is None


# ---------------------------------------------------------------------------
# profile subcommand + ssh --tty (v1.7.0): delegation seams for kora
# ---------------------------------------------------------------------------
def _boom(*a, **kw):
    raise AssertionError("must not be called")


def test_profile_prints_given_profile(monkeypatch, capsys):
    """`clouddevbox profile --profile p` echoes p on stdout with zero AWS work."""
    monkeypatch.setattr(cdb.sys, "argv", ["clouddevbox", "profile", "--profile", "p"])
    monkeypatch.setattr(cdb, "make_session", _boom)
    monkeypatch.setattr(cdb, "select_profile", _boom)
    assert cdb.main() == 0
    assert capsys.readouterr().out == "p\n"


def test_profile_delegates_to_select_profile(monkeypatch, capsys):
    monkeypatch.setattr(cdb.sys, "argv", ["clouddevbox", "profile"])
    monkeypatch.setattr(cdb, "make_session", _boom)
    monkeypatch.setattr(cdb, "select_profile", lambda: "picked")
    assert cdb.main() == 0
    assert capsys.readouterr().out == "picked\n"


def test_select_profile_single_message_on_stderr(monkeypatch, capsys):
    """The single-profile notice must stay off stdout - `profile` output is parsed."""
    monkeypatch.setattr(cdb, "_available_profiles", lambda: ["personal"])
    assert cdb.select_profile() == "personal"
    out, err = capsys.readouterr()
    assert out == ""
    assert "using the only configured AWS profile" in err


def test_parser_profile():
    args = cdb.build_parser().parse_args(["profile"])
    assert args.cmd == "profile" and args.profile is None
    args = cdb.build_parser().parse_args(["profile", "--profile", "p"])
    assert args.profile == "p"


def test_ssh_tty_flag(monkeypatch):
    class _TtyArgs(_Args):
        tty = True

    ran = []
    monkeypatch.setattr(cdb, "_SSH_KEY_CACHE", "/fake/key.pem")
    monkeypatch.setattr(cdb, "hs_nodes", lambda: {
        "devbox-x": {"id": "1", "online": True, "ip": "100.64.0.99",
                     "last_seen": None}})
    monkeypatch.setattr(cdb, "_tcp_reachable",
                        lambda h, p, t: (h, p) == (cdb.SOCKS_HOST, cdb.SOCKS_PORT))
    monkeypatch.setattr(cdb.subprocess, "run",
                        lambda cmd, **kw: (ran.append(cmd),
                                           type("R", (), {"returncode": 0})())[1])
    try:
        cdb.cmd_ssh(_TtyArgs(), None, None)
    except SystemExit as e:
        assert e.code == 0
    assert "-t" in ran[0]
    # default (_Args has no tty attr) stays pty-less
    ran.clear()
    try:
        cdb.cmd_ssh(_Args(), None, None)
    except SystemExit as e:
        assert e.code == 0
    assert "-t" not in ran[0]


def test_parser_ssh_tty():
    args = cdb.build_parser().parse_args(["ssh", "xb", "-t"])
    assert args.tty is True
    args = cdb.build_parser().parse_args(["ssh", "xb", "--tty", "--", "kora", "ssh"])
    assert args.tty is True and args.command == ["kora", "ssh"]
    args = cdb.build_parser().parse_args(["ssh", "xb"])
    assert args.tty is False
