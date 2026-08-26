# cn-cli-devbox — `clouddevbox`

CLI for on-demand cloud devboxes in AWS `us-east-2`, built on
[cn-cdk-devbox](https://github.com/GonzaloAlvarez/cn-cdk-devbox) stacks.
Boxes have **no open ports at all** (empty security-group ingress): access is
SSH **over the headscale tailnet** or **SSM Session Manager**, nothing else.

Installed via [gear](https://github.com/GonzaloAlvarez/gear)
(`com/clouddevbox`) to `~/bin/clouddevbox`. Single-file Python; every run
creates a throwaway venv under `./.clouddevbox` in the current directory
(boto3 + bullet; the venv's own pip is upgraded so it never nags) and
deletes it on exit — amun-style, nothing persists between runs
(costs ~10-20 s per invocation).

## Prerequisites

- An AWS profile in `~/.aws/config` / `~/.aws/credentials`. Every command
  takes `--profile`; **when omitted, a sole configured profile is used
  automatically (tty or not), and with more than one an interactive menu
  (bullet) lists them on a tty** — there is still no default and no env
  fallback beyond that, so scripts on multi-profile machines must pass it.
  SSO profiles work; on
  expiry the CLI tells you to `aws sso login`.
- `node` on PATH (asdf/brew) — only needed by `new` (runs `cdk deploy`).
- `session-manager-plugin` on PATH for `ssm` — install via gear:
  `~/.gear/com/session-manager-plugin/setup-darwin` (or `setup-debian`).
- An **ssh key that authenticates as `gonzalo@hs.gn.al`** (also needed by
  `new`/`list`/`destroy`/`start`/`status`, which ssh there for headscale).
  **New-machine setup is two commands**: `kauket request
  ssh.gonzalo_main_private_key` (enrolls this machine; approve from the
  admin machine), then `kauket get ssh.gonzalo_main_private_key` — it
  installs the master key at the canonical path the CLI checks first.
  Per run, the CLI uses the first key that actually authenticates to the
  VPS:
  1. `CLOUDDEVBOX_SSH_KEY` env var, if set (trusted as-is);
  2. the **kauket-canonical paths** `~/.ssh/gonzalo_main_private_key` and
     `~/.ssh/gonzalo_main_private_key.pem` — no `.pub` or comment needed;
  3. every other `~/.ssh` key **tagged `gonzaloab@gmail.com`** — in its
     `.pub` comment, or its embedded comment when there is no `.pub`;
  4. every **kauket `ssh.*` secret** granted to this machine — fetched with
     `kauket get --stdout` (`--no-sync` fast path, sync retry) into a `0700`
     tmp dir (`0600` files) deleted on exit, probed the same way; nothing
     is installed into `~/.ssh`. Uses `$KAUKET_HOME` if set, else the
     default kauket client home (`~/Library/Application Support/kauket` on
     macOS, `~/.config/kauket` elsewhere), else the legacy
     `~/.config/kauket-operator-client`.

  To make some other local key discoverable, give it a `.pub` whose comment
  holds the email:
  `echo "$(ssh-keygen -y -f <key>) gonzaloab@gmail.com" > <key>.pub`.
- Tailnet SSH also needs a path to the tailnet: the cn-socksnode proxy on
  `127.0.0.1:1055` (`~/dev/cn-socksnode/run.sh` — the Mac's only tailnet
  doorway, at home or away), or a native tailnet route (e.g. running
  clouddevbox on one devbox to reach another). `ssh` picks automatically:
  proxy when the port is up, direct otherwise, and suggests `ssm` when
  neither exists.
- `new`/`destroy` talk to headscale over plain SSH to `hs.gn.al` (public).
- A `cn-cdk-devbox` checkout at `~/dev/cn-cdk-devbox` (or set
  `CLOUDDEVBOX_CDK_REPO`); if neither exists, `new` clones it temporarily.

## Usage

```sh
clouddevbox new alpha --profile personal              # m7g.large, 50 GB, 6h autostop
clouddevbox new beta  --profile personal --type m7g.xlarge --disk 100 \
                      --plugins kauket,docker --autostop 10h
clouddevbox list --profile personal
clouddevbox list                                      # no --profile: interactive picker
clouddevbox ssh alpha --profile personal              # tailnet, via socks proxy
clouddevbox ssh alpha --profile personal --show       # also print the raw ssh command
clouddevbox ssh alpha --profile personal -- uname -a
clouddevbox ssm alpha --profile personal              # Session Manager (break-glass)
clouddevbox status alpha --profile personal           # provisioning/autostop/tailnet state
clouddevbox stop alpha --profile personal             # EBS-only cost while stopped
clouddevbox start alpha --profile personal
clouddevbox autostop alpha 10h --profile personal     # or: 90m, off
clouddevbox destroy alpha --profile personal          # stack + node + params, confirmed
```

`list`, `stop`, `start` are driven purely by EC2 instance tags
(`devbox:name`, `devbox:managed-by=clouddevbox`); `new` and `destroy` operate
on the CloudFormation stack `Devbox-<name>` (destroy uses the CFN API
directly — no cdk toolchain needed).

## Naming

Every live devbox resolves as **`<name>.devbox.lab.gn.al`** on the tailnet
(and via the socks proxy in default mode). The VPS CoreDNS serves a
`devbox.lab.gn.al` zone that rewrites to the MagicDNS name
`devbox-<name>.ts.gn.al` — stateless, so live boxes resolve and destroyed
ones NXDOMAIN. Caveat: with cn-socksnode in `--exit-lan` mode, `lab.gn.al`
split-DNS is bypassed (like every tailnet-only lab name) — `clouddevbox
ssh` is immune (it connects by tailnet IP).

## What `new` actually does

1. Mints a **one-shot headscale preauth key** (tag:devbox, 1 h expiry) via
   `ssh hs.gn.al → docker exec cloudnet-headscale-1 headscale preauthkeys
   create`.
2. Stages it as SSM SecureString `/devbox/<name>/ts-authkey`.
3. Runs `cdk deploy DevboxBase Devbox-<name>` in the cn-cdk-devbox checkout
   (throwaway `.venv`/`.npm` toolchain, removed after).
4. Waits: instance running → SSM agent online → tailnet node online.
5. **Deletes the SSM parameter** (also on failure paths).

The instance's user-data consumes the key at first boot: SSM agent → user →
autostop timer → amun `tailscale` → `tailscale up
--login-server=https://hs.gn.al --accept-routes` → `pki.lan` hosts pin →
**amun core** (step-ca trust, ufw deny-in/allow-22, dotfiles, sshd
hardening) → the `--plugins` list. Progress: `clouddevbox status <name>`,
full log on the box at `/var/log/devbox-bootstrap.log`.

`status` reports a provisioning state: **`provisioning`** while amun core +
plugins are still installing (with the per-stage done/wanted lists on boxes
created after 2026-07-22), **`running (fully provisioned: …)`** once every
requested stage finished, or **`provisioning FAILED at: …`** with the step
that broke. A box is SSH-able as soon as it joins the tailnet, several
minutes before it reaches `running`.

## Bootstrap customization (`--plugins`)

`--plugins` is a comma-separated list of [amun](https://github.com/GonzaloAlvarez/amun)
plugins run after amun core, each as `bash <(curl -fsSL https://go.gn.al/amun) <plugin>`
(i.e. the `amun-<plugin>` repo). Default: `kauket`.

- `--plugins kauket,docker` → kauket binary + Docker/Compose
- `--plugins ""` → core only
- kauket **enrollment** is always manual (interactive device flow):
  `kauket enroll --request host.devbox-<name>` on the box, `kauket approve`
  on the Mac.

## Autostop

Every box self-**stops** (never terminates) once its uptime exceeds
`/etc/clouddevbox/autostop` — default `6h`, checked every 5 min by
`devbox-autostop.timer` on the box, measured from each boot. A stopped box
costs only its EBS volume (≈$4/mo at 50 GB gp3). `clouddevbox autostop
<name> <value>` rewrites the setting live over SSM (`10h`, `90m`, `off`);
it persists across stop/start.

## Costs

| State | Cost |
|---|---|
| running m7g.large | ≈ $0.082/h |
| stopped | EBS only, ≈ $0.08/GB·mo (≈$4/mo at 50 GB) |
| destroyed | $0 |
| shared base (VPC/SG/role) | $0 (no NAT, no EIP) |

## Caveats

- **One `new` at a time** — concurrent runs both update the shared
  `DevboxBase` stack and CloudFormation rejects overlapping updates.
- **Headscale node keys expire** (~180 d default): a very long-lived box
  eventually drops off the tailnet; SSM still works. Re-auth by minting a
  key and re-running `tailscale up` on the box (a `clouddevbox reauth` is
  future work).
- The instance role can read any `/devbox/*` parameter, not just its own —
  accepted: keys are one-shot, expire in 1 h, and are deleted right after
  the join.
- SSM sessions land as `ssm-user`; use `sudo su - gonzalo`.
- Never `cdk deploy` an existing box from a fresh checkout — see the
  AMI-drift warning in the cn-cdk-devbox README (instance replacement).

## Termux (Android)

Works on Termux — gear installs it via `~/.gear/run clouddevbox` (which
dispatches `com/clouddevbox/setup-termux`), and the self-venv bootstrap runs
on the Termux python (amun installs `python-ensurepip-wheels`, so
`python -m venv` seeds pip; boto3/bullet are pure-python).

- **Supported**: everything except `ssm` — there are no aws CLI v2 /
  session-manager-plugin builds for bionic. Use `clouddevbox ssh`.
- **Tailnet path**: install the Tailscale Android app and log into headscale;
  its kernel VPN covers Termux sockets, so the direct-to-tailnet-IP branch of
  `ssh` works — no `nc`/SOCKS proxy needed on the phone.
- **SSH key**: set `CLOUDDEVBOX_SSH_KEY=~/.ssh/<key>` to bypass kauket key
  discovery (kauket runs on Termux, but pure-Go DNS resolution can fail on a
  real device — no `/etc/resolv.conf` outside containers).
- Run from a writable CWD (e.g. `$HOME`) — the throwaway `.clouddevbox` venv
  is created in the current directory and `/sdcard` is noexec.
