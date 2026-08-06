# CI runners

`.github/workflows/publish-images.yml` builds the BioFlow container images and
publishes them to GHCR. It runs entirely on **self-hosted runners** — there are
no GitHub-hosted jobs in this repo.

That is not a preference, it is a constraint. This repository is private and
owned by a personal account, and GitHub's hosted arm64 Linux runners
(`ubuntu-24.04-arm`) are not available on that plan. The alternatives were
QEMU emulation — which [#46](https://github.com/syntheticgio/bioflow/issues/46)
measured as hours long and liable to fail partway, because `backend/Dockerfile`
compiles bwa-mem2 from source, installs Clair3, and builds compleasm — or
publishing amd64 only and letting arm64 drift, which is the exact problem
[#38](https://github.com/syntheticgio/bioflow/issues/38) exists to prevent.

## What the workflow expects

| Label pair | Machine | Builds |
| --- | --- | --- |
| `self-hosted`, `X64` | `MainLinux` (amd64 Linux box) | `linux/amd64`, plus both manifest-merge jobs |
| `self-hosted`, `ARM64` | Apple Silicon dev machine | `linux/arm64` |

Both need a working Docker daemon. Nothing else is installed by the workflow —
`docker/setup-buildx-action` brings its own buildx binary.

**Jobs queue rather than fail when a runner is offline.** If the Mac is asleep,
a push to `main` leaves the two `arm64` build jobs (and therefore both
`manifest` jobs) pending until it wakes. The amd64 half completes on its own.
This is the accepted cost of the choice above; the alternative was emulation on
every push.

## Registering the ARM64 runner

On the Apple Silicon machine, with Docker Desktop installed:

1. In the repository, go to **Settings → Actions → Runners → New self-hosted
   runner**, and pick **macOS** / **arm64**. The page generates a registration
   token, which expires in an hour.
2. Follow the download and `./config.sh` steps it prints verbatim. Accept the
   default labels — the runner self-reports `self-hosted`, `macOS`, and
   `ARM64`, and the workflow keys off `ARM64`.
3. Install it as a service so it survives logout of the terminal:

   ```bash
   ./svc.sh install
   ./svc.sh start
   ```

4. Confirm it appears online:

   ```bash
   gh api repos/:owner/:repo/actions/runners --jq '.runners[] | "\(.name) \(.status) \(.labels | map(.name) | join(","))"'
   ```

**Docker Desktop must be running for the runner's jobs to work, and on macOS
that means a logged-in GUI session.** `svc.sh` installs a LaunchAgent, not a
LaunchDaemon, so the runner starts at user login rather than at boot — but
Docker Desktop has the same requirement, so the two agree. A Mac sitting at the
login window has neither, and its jobs queue as described above.

The runner being *online* is not evidence that Docker is. The runner registers
and accepts jobs perfectly well without it, then fails about thirty seconds in
with:

```
ERROR: failed to initialize builder bioflow (bioflow0): failed to connect to the
docker API at unix:///Users/<user>/.docker/run/docker.sock; check if the path is
correct and if the daemon is running: dial unix ...: no such file or directory
```

That socket path is Docker Desktop's own, and it does not exist until Docker
Desktop starts. Turn on **Start Docker Desktop when you sign in** in its
settings, so a reboot doesn't quietly take arm64 builds offline while leaving
the runner showing green in the Actions UI.

## GHCR package access

The workflow's jobs request `packages: write`, but that only authorizes the
*token*. A GHCR package separately has to admit the repository, and the two
packages here were first pushed by hand with a personal access token under the
user namespace ([#37](https://github.com/syntheticgio/bioflow/issues/37)), which
links them to no repository at all.

The symptom is a push that runs to completion and then fails on the last step:

```
ERROR: failed to solve: failed to push ghcr.io/syntheticgio/bioflow-web: denied: permission_denied: write_package
```

That is a *package permission* problem, not a token-scope or workflow-syntax
one, and no amount of editing the workflow fixes it. It is a one-time manual
grant, per package:

1. Go to the package settings —
   `https://github.com/users/syntheticgio/packages/container/bioflow-backend/settings`
   and the same for `bioflow-web`.
2. Under **Manage Actions access**, choose **Add repository**, pick
   `syntheticgio/bioflow`, and set the role to **Write**.

The workflow also stamps `org.opencontainers.image.source` on every build, which
is what keeps the link in place afterwards and makes the package page point back
at the repo. That label cannot bootstrap the link on its own, though — applying
it requires a successful push, which is the thing being blocked.

## Layer caching

Both runners persist their BuildKit state between jobs. The workflow's
`docker/setup-buildx-action` step passes a fixed builder `name` plus
`keep-state: true` — documented upstream as "only useful on persistent
self-hosted runners," which is precisely this case. Without it every run starts
from a cold cache and recompiles bwa-mem2 from source.

GitHub Actions cache (`cache-to: type=gha`) is not usable here: the limit is
10GB for the whole repository and the backend image alone is about 7.9GB.

If a build ever behaves as though the cache is poisoned, drop the builder on
the affected runner and let the next run recreate it:

```bash
docker buildx rm bioflow
```
