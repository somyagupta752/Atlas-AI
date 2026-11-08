# Worker host validation plan (staging only)

Purpose: decide whether the rootless worker design (`rootless-worker-design.md`) is safe enough to run untrusted
repositories. **Staging VM only.** Do not run any phase against production, and do not accept hostile repositories
anywhere until section "Go / no-go" is signed.

Nothing here has been executed on a Linux VM. The automated checks were exercised once against Docker Desktop on macOS to
prove the test logic: 9 passed, 19 skipped (the rootless, ACL and proxy checks cannot run there). That run says nothing
about rootless behaviour.

## Rules for the run

- A fresh Linux VM (Debian 12 or Ubuntu 22.04+, kernel 5.11+), snapshotted before Phase 1 so it can be reset.
- No real secrets on it. Use throwaway database and signing keys for staging.
- Run the checks as an **operations account** (`ops`), not `atlas-worker` and not root.
- Record every command and its output in the evidence folder `validation-<date>/`. A phase with no recorded output did not
  happen.
- Any failing check in Phases 2 to 4 stops the run. Fix the cause, reset to the snapshot, and rerun from Phase 1.

## Phase 1 - Build the host

| Step | Command / action | Evidence |
| --- | --- | --- |
| 1.1 | `stat -fc %T /sys/fs/cgroup` prints `cgroup2fs`; `uname -r` is 5.11 or newer | output |
| 1.2 | Install `uidmap dbus-user-session slirp4netns systemd-container acl curl` | `dpkg -l` lines |
| 1.3 | `sudo useradd --create-home --shell /usr/sbin/nologin atlas-worker`; confirm no sudo, no supplementary groups: `id atlas-worker` | `id` output |
| 1.4 | `/etc/subuid` and `/etc/subgid` each contain `atlas-worker:100000:65536` (record `SUBUID_START`) | file lines |
| 1.5 | Delegate cgroup controllers: `/etc/systemd/system/user@.service.d/delegate.conf` with `Delegate=cpu cpuset io memory pids`; `systemctl daemon-reload`; reboot | file, `cat /sys/fs/cgroup/user.slice/user-$(id -u atlas-worker).slice/cgroup.controllers` lists `memory pids cpu` |
| 1.6 | `sudo loginctl enable-linger atlas-worker` | `loginctl show-user` shows `Linger=yes` |
| 1.7 | As `atlas-worker`: install rootless Docker with `dockerd-rootless-setuptool.sh install`, `systemctl --user enable --now docker` | `systemctl --user status docker` |
| 1.8 | Install rootful Docker Engine for the stack (daemon A) | `docker version` |
| 1.9 | Build the worker image in daemon B: `DOCKER_HOST=unix:///run/user/$(id -u atlas-worker)/docker.sock docker build -f docker/test-runner.Dockerfile -t atlas-ai-test-runner:py3.12.13-pytest9.2.0 docker`; record the digest | `docker image inspect` |
| 1.10 | Workspace tree and ACLs exactly as in the design; record `getfacl` | `getfacl` output |
| 1.11 | Confirm `atlas-worker` cannot read the stack's secrets: `sudo -u atlas-worker cat <secrets>/auth_secret_key` fails | error text |
| 1.12 | Confirm nothing else listens: `ss -tlnp` shows only SSH and the dashboard bound to loopback | output |

## Phase 2 - Automated checks (daemon B)

```
export DOCKER_HOST=unix:///run/user/$(id -u atlas-worker)/docker.sock    # the ops account needs access to this socket
export ATLAS_WORKER_VALIDATION=1 ATLAS_EXPECT_ROOTLESS=1
export ATLAS_WORKSPACE_ROOT=/var/lib/atlas-workers/workspaces
uv run pytest tests/security/test_worker_host.py -v -rs | tee validation-<date>/phase2.txt
```

Required: **no failures and no skips** other than the proxy checks (run those in Phase 3). A skip means a check did not run.

| ID | Checks | Condition |
| --- | --- | --- |
| V1 | Daemon reports `rootless`; its process is not root; the run itself is not root | 1 |
| V2 | cgroup v2; memory, swap, pids and cpu limits are visible as set from inside a container | 5 |
| V3 | Host canary path, Docker sockets, secrets paths and `/Users` are absent; environment has no host secrets; no capabilities; no-new-privileges | 2, 5 |
| V4 | Only the workspace and `/tmp` are writable; `/tmp` is size-limited; a symlink to a host path exposes nothing | 3, 5, 6 |
| V5 | No interface is up except loopback; no route; connections to the internet, cloud metadata, the Docker gateway and local ports fail; DNS fails | 5, 6 |
| V6 | Memory bomb killed by the limit; fork loop stopped at the pids limit; large write stopped by `fsize`; infinite loop killed by the timeout; no container left behind | 5, 6 |
| V8 | Files and directories the worker creates are readable and deletable by the API account; their owner is a mapped sub-uid; the workspace root is not accessible to other users | 4 |
| V9 | A **privileged** container with `-v /:/host` cannot read `/etc/shadow`, `/root`, the ops account's `0600` canary, the SSH host keys or the rootful daemon's data, and cannot write to `/etc` or `/usr/bin` | 7 |

V9 is the decisive test for condition 7: it is what a compromised API could ask for. Any "ESCAPE" message in its output
is an automatic no-go.

## Phase 3 - Stack and proxy

Start the stack on the VM from a staging `deploy.env` with the design's compose changes applied (they are not applied in
the repository yet). This is the first run of `docker-compose.prod.yml` anywhere, so also record its ordinary behaviour.

| Step | Check | Pass |
| --- | --- | --- |
| 3.1 | `docker compose ... ps`: all healthy, `migrate` exited 0 | yes |
| 3.2 | `curl -fsS 127.0.0.1:8080/ready` returns `ready`; the response lists `worker_backend: ok` | yes |
| 3.3 | Publish the proxy port temporarily on loopback only, then `ATLAS_PROXY_URL=http://127.0.0.1:2375 pytest ... -k v10` | 14 refusals are 403; allowed calls succeed |
| 3.4 | From inside the API container: `ls /var/run/docker.sock` and `docker -H unix:///var/run/docker.sock ps` fail; only `DOCKER_HOST=tcp://docker-proxy:2375` works | yes |
| 3.5 | From inside the API container: `docker run --rm --privileged -v /:/host alpine cat /host/etc/shadow` **through the proxy** fails or returns nothing (condition 7, end to end) | yes |
| 3.6 | From inside the API container: a request to the daemon-A socket, the databases' ports from `frontend`, and the internet from `postgres` all fail | yes |
| 3.7 | One real run: bug-001 reference evaluation through the dashboard API, container backend, `PATCH_CONTAINER_ALLOW_UNTRUSTED=true`; verified, held-out passes, workspace removed | yes |
| 3.8 | `docker stats` during 3.7: the worker stays inside its memory and pids limits | yes |

## Phase 4 - Adversarial and failure checks (manual)

Use a purpose-written hostile repository (never a real one). Each row needs a recorded outcome.

| # | Attack | Expected |
| --- | --- | --- |
| 4.1 | Test that reads `/proc/1/environ`, `/proc/self/mountinfo`, `/sys/fs/cgroup`, and tries `mount`, `unshare`, `ptrace`, `keyctl`, `bpf` | denied or empty; nothing from the host |
| 4.2 | Test that writes 1 GB to the workspace | bounded by `fsize` or the workspace quota; the VM disk is not filled; run fails cleanly |
| 4.3 | `conftest.py` that runs at collection time and spawns a long-lived background process | killed with the container at timeout; no process survives (`ps -u atlas-worker`) |
| 4.4 | Test that tries to reach the rootless daemon socket path and the API container by name and IP | unreachable |
| 4.5 | Patch that adds a symlink to `/etc/passwd` and one to the workspace parent | rejected by validation; if applied, reads nothing |
| 4.6 | Test that tries to read another run's workspace while two runs are active | **Expected to be possible** if it escapes the container; record whether a normal (non-escaped) container can see it (it must not) |
| 4.7 | `kill -9` the API during a run | on restart, the orphan worker and workspace are removed; unrelated containers on daemon B remain |
| 4.8 | `systemctl --user stop docker` (daemon B) mid-run | the run fails with a backend error; `/ready` turns `not_ready`; nothing is recorded as passed |
| 4.9 | Reboot the VM mid-run | no worker restarts; startup cleanup removes leftovers; the evaluation closes as `partial` |
| 4.10 | Cancel an evaluation | the container disappears within seconds (`docker ps` on daemon B) |
| 4.11 | Run 4 evaluations at once (the stack allows one; confirm the second is refused) and 8 concurrent patch requests | no shared files, no leaked containers, memory stays under the host limit |
| 4.12 | Fill the workspace filesystem to 95% | Atlas refuses new workspaces (`min_free_bytes`), existing runs fail cleanly |

## Phase 5 - Soak

Run the reference benchmark (13 cases) 20 times back to back. Pass: identical verdicts every time, no leaked containers or
workspaces, no growth of daemon B's disk beyond the image and the cache (check `docker system df`), logs contain no
secret (grep the captured log for the throwaway key values).

## Go / no-go

All must be true:

- [ ] Phase 1 complete with evidence; `atlas-worker` cannot read any application secret.
- [ ] Phase 2: V1-V9 pass with zero failures and zero skips.
- [ ] Phase 3: 3.1-3.8 pass, in particular 3.4 and 3.5.
- [ ] Phase 4: every row has the expected result; any row 4.x that differs is explained and accepted in writing.
- [ ] Phase 5 passes.
- [ ] Residual risks in `rootless-worker-design.md` are reviewed and accepted by the owner.
- [ ] The kernel and Docker versions tested are recorded and a patching schedule exists.

**No-go triggers (any one):** a V9 "ESCAPE"; a limit that is not enforced (V2, V6); V8 cannot pass without world-writable
modes; a worker reaches the network; an application secret is readable by `atlas-worker`; cleanup leaves a container after
a crash. A no-go means the design changes (see "When this design is wrong" in the design document), not that the test is
relaxed.

## What this plan does not cover

- Kernel zero-day exploitation. Rootless limits the damage; it does not prevent the exploit. For hostile multi-tenant use,
  evaluate gVisor or Kata separately.
- Multi-tenant isolation between users' runs: workers share one account.
- Network egress filtering of the VM itself (needed to limit image pulls through the proxy).
- Off-host backups and failure alerting, CI/CD and browser end-to-end tests: separate work items.

## Evidence template

```
validation-<date>/
  host.txt            uname -a, /etc/os-release, docker version (both daemons), subuid/subgid, getfacl
  phase1.txt ... phase5.txt
  hostile-repo/       the repository used in Phase 4
  signoff.md          the go / no-go checklist above, each item with a link to its evidence, name and date
```
