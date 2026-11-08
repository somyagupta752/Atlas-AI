# Rootless worker design

Status: **design, not validated.** Nothing in this document has been run on a Linux VM. The validation plan
(`worker-validation-plan.md`) decides whether it is accepted. Until it passes, Atlas must not run untrusted
repositories. Docker Desktop on macOS is not a substitute: it has no rootless mode and a different kernel.

## Decision

Test workers run under a **rootless Docker daemon** owned by a dedicated, unprivileged account, on a **dedicated
Linux VM**. The current worker interface (`ContainerTestRunner`) and its bind-mount architecture are kept. Remote
workspace streaming is not introduced unless validation shows the bind-mount design cannot work (see "When this design is
wrong").

## Why this reduces risk, and what it does not remove

In a rootful daemon, a container that asks for `--privileged` or `-v /:/host` reaches the host as root. In a rootless
daemon the whole daemon, and every container it starts, runs inside a user namespace as the account `atlas-worker`.
"Root" inside a container maps to that unprivileged account, so the same request gives access to the files and
resources of `atlas-worker` and nothing more.

| Threat | Rootful + proxy | Rootless, dedicated account |
| --- | --- | --- |
| API compromised, asks for a privileged container | Host root | Capabilities inside a user namespace; host files only as `atlas-worker` |
| ...with `-v /:/host` | Reads and writes the whole host | Only what `atlas-worker` can read or write |
| Kernel or runtime vulnerability | Host root | Host as `atlas-worker`, then a second escalation is needed |
| Container reads other runs' workspaces | Possible | Possible: they share the account (see residual risks) |

It does **not** make arbitrary code harmless. A kernel exploit still reaches the VM as `atlas-worker`. The account must
therefore own nothing worth stealing, and the VM must hold nothing else.

## Topology

One VM, two Docker daemons that cannot see each other's data:

```
                         dedicated Linux VM (nothing else runs here)
 +--------------------------------------------------------------------------------------+
 |  Daemon A (rootful, Docker Engine)             Daemon B (rootless, account atlas-worker)|
 |   api, frontend, postgres, neo4j, backup,       worker containers only                 |
 |   docker-proxy                                  socket: /run/user/<W>/docker.sock      |
 |        |                                              ^                                 |
 |        | tcp (internal network `workers`)             | unix socket, mounted ONLY into |
 |        +------------> docker-proxy -------------------+ the proxy container            |
 |                                                                                        |
 |   /var/lib/atlas-workers/workspaces  (account atlas-worker; ACLs for the API uid)      |
 +--------------------------------------------------------------------------------------+
```

- Daemon A runs the stack in `docker-compose.prod.yml`. It holds every application secret, in Docker secrets and
  volumes that `atlas-worker` cannot read (mode 0400, other owners).
- Daemon B holds no application secret, no database and no Neo4j. It has its own image store and containers.
- The API reaches daemon B only through the filtering proxy, over an internal network. The proxy is the only
  container with the daemon-B socket. The API has no socket, no `docker.sock` mount and no route to the VM's files other
  than the workspace directory and the read-only repositories directory.

### Where the original conditions land

| # | Condition | How it is met |
| --- | --- | --- |
| 1 | Daemon under a dedicated unprivileged account | `atlas-worker`: no sudo, no login shell, no supplementary groups, `systemd --user` with lingering |
| 2 | VM dedicated, no application secrets | The VM is dedicated to Atlas. **Adjustment:** the API's own secrets (database URL, signing key) live on this VM, in daemon A, because the API runs here. They are unreadable by `atlas-worker`. A VM with *no* secrets at all requires the API to be elsewhere, which requires remote workspace streaming; that is the alternative below. |
| 3 | API cannot access the VM filesystem or unrestricted daemon access | The API is a container with two bind mounts (workspaces, repositories read-only). Its daemon access is the proxy to a rootless daemon, so "unrestricted" is bounded by `atlas-worker`'s privileges |
| 4 | Workspace mounts work with rootless UID mapping | ACL design below. **Unproven** until V8 passes |
| 5 | Network isolation, limits, cleanup preserved | Same `docker run` flags. Rootless needs cgroup v2 with controllers delegated or the limits are silently ignored. V2 proves they bite |
| 6 | Tests for host files, escapes, network, exhaustion | `tests/security/test_worker_host.py` and the manual checks in the plan |
| 7 | Compromised API cannot make privileged containers or mount sensitive directories | Cannot be *prevented* at the API (the daemon honours create requests). It is made *harmless*: V9 shows a privileged container with `-v /:/host` cannot read root-owned files or any secret |

Condition 7 is satisfied by containing the damage, not by forbidding the request. Say so in sign-off.

## UID mapping and the workspace (the part most likely to fail)

Inside a rootless container, uid 0 is `atlas-worker` (host uid `W`) and container uid `n` (n ≥ 1) is host uid
`SUBUID_START + n - 1`, where `SUBUID_START` is the first entry for `atlas-worker` in `/etc/subuid`. The code starts the
worker with `--user <uid>:<gid>` of the API process: **10001:10001**. That container user is therefore host uid
`SUBUID_START + 10000`. It matches no file owner, so a plain bind mount would be unwritable and the JUnit output
could not be written.

The workspace directory is created by the API (uid 10001 in daemon A's namespace, which is host uid 10001 when the
Compose stack is not user-namespaced). Access is granted with POSIX ACLs, not with broad modes:

```
install -d -o atlas-worker -g atlas-worker -m 0700 /var/lib/atlas-workers
install -d -o 10001 -g atlas-worker -m 0770 /var/lib/atlas-workers/workspaces
setfacl    -m u:10001:rwx,u:$((SUBUID_START+10000)):rwx /var/lib/atlas-workers/workspaces
setfacl -d -m u:10001:rwx,u:$((SUBUID_START+10000)):rwx /var/lib/atlas-workers/workspaces
```

- The default ACL makes every new workspace directory inherit both identities, so the API can create and later delete
  them and the unprivileged worker uid can write results.
- No other host user has access (mode 0770, group `atlas-worker` only). `atlas-worker` itself reaches the tree through the
  group.
- Files written by the worker are owned by the mapped subuid. The API removes them through the ACL on the parent
  directory. V8 proves create, read and delete from both sides.

If ACLs are unavailable on the filesystem, the fallback is a setgid group directory (`2770`) with the worker user's
primary group set by `--group-add`. If neither works, the design is wrong (below).

## Resource limits

Rootless Docker applies `--memory`, `--cpus` and `--pids-limit` only if cgroup v2 is mounted and the controllers are
delegated to the user's systemd slice; otherwise Docker prints a warning and runs the container *without the limit*.
Required on the VM:

```
# /etc/systemd/system/user@.service.d/delegate.conf
[Service]
Delegate=cpu cpuset io memory pids
```

`ulimit` settings (`nofile`, `fsize`), `--read-only`, `--tmpfs` size, `--network none`, `--cap-drop ALL`,
`no-new-privileges`, the host-side timeout and `docker rm -f` do not depend on delegation. V2 and V6 check the limits from
inside a container rather than trusting `docker info`.

## Installing the daemon (outline; exact commands in the plan)

1. Debian 12 or Ubuntu 22.04+ with kernel 5.11+ and cgroup v2 (`stat -fc %T /sys/fs/cgroup` prints `cgroup2fs`).
2. Packages: `uidmap`, `dbus-user-session`, `slirp4netns` (or `passt`), `systemd-container`, `acl`.
3. Account `atlas-worker` with a `/etc/subuid` and `/etc/subgid` range of at least 65536, `loginctl enable-linger`.
4. As that account: `dockerd-rootless-setuptool.sh install`, then `systemctl --user enable --now docker`.
5. Load the worker image **into this daemon** (images are per daemon): `docker build -f docker/test-runner.Dockerfile ...`
   with `DOCKER_HOST=unix:///run/user/<W>/docker.sock`.
6. Pin `WORKER_IMAGE` by digest in `deploy.env`.

## Changes to the repository (not applied until the design is accepted)

| File | Change |
| --- | --- |
| `docker-compose.prod.yml` | `docker-proxy`: `user: "${WORKER_UID}:${WORKER_GID}"` so it can open the rootless socket; `DOCKER_SOCKET` points to `/run/user/<W>/docker.sock`; remove any reference to the rootful socket. `WORKSPACE_HOST_DIR` becomes `/var/lib/atlas-workers/workspaces` |
| `deploy.env.example` | `WORKER_UID`, `WORKER_GID`, the new paths |
| `docs/deployment.md` | The VM preparation steps and a link to this document |
| `app/patching/container.py` | None expected. If V8 shows the `--user` mapping is wrong, add a `PATCH_CONTAINER_USER` setting rather than changing the default |
| `/ready` | Report `worker_rootless: ok` after checking `docker info` for the rootless security option, so a rootful daemon behind the proxy is visible |

## Residual risks

- **Workers share one account.** A hostile run that escapes its container can read other runs' workspaces while they
  exist. Workspaces are deleted after each run and are not secret from each other only because Atlas is single-tenant
  today. Multi-tenant use needs one account (or VM) per tenant.
- **Kernel attack surface.** User namespaces enlarge it. Keep the kernel patched; consider gVisor (`runsc`) or Kata for
  hostile multi-tenant workloads.
- **The proxy remains in the path** and is a root-equivalent target on daemon A only if daemon A is compromised through
  it; it holds the B socket, not the A socket.
- **Image pull.** The proxy permits image creation, so a compromised API can pull images through the VM's network
  egress. Restrict VM egress to the registries you use.
- **Availability.** A hostile workload can still fill the workspace disk. Put the workspace on its own filesystem or
  quota (`xfs` project quota) and alert on usage.

## When this design is wrong

Stop and revisit remote workers if validation shows any of: V8 cannot be made to pass with ACLs or groups; limits cannot
be enforced under rootless on the chosen VM image; the API cannot be kept on the VM without exposing its secrets to
`atlas-worker`. Then the options are a worker service on its own VM with workspaces streamed to it, or gVisor/Kata on the
same VM.
