# cn-cli-devbox — `clouddevbox`

CLI for on-demand cloud devboxes in AWS `us-east-2`, built on
[cn-cdk-devbox](https://github.com/GonzaloAlvarez/cn-cdk-devbox) stacks.
Boxes have **no open ports at all** (empty security-group ingress): access is
SSH **over the headscale tailnet** or **SSM Session Manager**, nothing else.

Installed via [gear](https://github.com/GonzaloAlvarez/gear)
(`com/clouddevbox`) to `~/bin/clouddevbox`. Single-file Python; every run
creates a throwaway venv under `./.clouddevbox` in the current directory
(boto3) and deletes it on exit — amun-style, nothing persists between runs
(costs ~10-20 s per invocation).

## Prerequisites

- An AWS profile in `~/.aws/config` / `~/.aws/credentials`. **`--profile` is
  mandatory on every command** — there is no default and no env fallback.
  SSO profiles work; on expiry the CLI tells you to `aws sso login`.
- `node` on PATH (asdf/brew) — only needed by `new` (runs `cdk deploy`).
- `session-manager-plugin` on PATH for `ssm` — install via gear:
  `~/.gear/com/session-manager-plugin/setup-darwin` (or `setup-debian`).
- Tailnet SSH needs the cn-socksnode proxy up on `127.0.0.1:1055`
  (`~/dev/cn-socksnode/run.sh`) and `~/.ssh/gonzalo_main_private_key.pem`.
- `new`/`destroy` talk to headscale over plain SSH to `hs.gn.al` (public).
- A `cn-cdk-devbox` checkout at `~/dev/cn-cdk-devbox` (or set
  `CLOUDDEVBOX_CDK_REPO`); if neither exists, `new` clones it temporarily.

## Usage

```sh
clouddevbox new alpha --profile personal              # m7g.large, 50 GB, 6h autostop
clouddevbox new beta  --profile personal --type m7g.xlarge --disk 100 \
                      --plugins kauket,docker --autostop 10h
clouddevbox list --profile personal
clouddevbox ssh alpha --profile personal              # tailnet, via socks proxy
clouddevbox ssh alpha --profile personal -- uname -a
clouddevbox ssm alpha --profile personal              # Session Manager (break-glass)
clouddevbox status alpha --profile personal           # bootstrap/autostop/tailnet state
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
