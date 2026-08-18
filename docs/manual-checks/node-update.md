# Manual check: a compute node update

Issue [#474](https://github.com/syntheticgio/bioflow/issues/474), check 4's UI
half. The backend half of both of #474's checks is automated in
`backend/tests/integration/test_node_update_live.py`; what remains here is what
only a person looking at the screen can answer.

Design spec:
[`docs/superpowers/specs/2026-08-12-node-update-design.md`](../superpowers/specs/2026-08-12-node-update-design.md).

## What this check is for

Three properties of the update UI, none of which any test in this repo asserts:

1. The drain prompt appears **only** when jobs are running on the node.
2. The progress panel advances through pull → restart → verify.
3. The node's row shows the new version once the worker re-enrolls.

Property 3 is the one that still wants a genuine second machine, because a real
worker re-enrolling is the step the container harness simulates rather than
performs. Properties 1 and 2 can be observed against the containerized node
below.

## Setup

The same sidecar the automated tests use, run by hand so the UI can talk to it.
From this worktree:

```bash
./ops/worktree-up.sh
```

Then start the node sidecar on the stack's network:

```bash
docker run -d --rm --name manual-node --network biopipe_default -e USER_NAME=bioflow -e USER_PASSWORD=testpw -e PASSWORD_ACCESS=true -e PUID=1000 -e PGID=1000 -v /var/run/docker.sock:/var/run/docker.sock lscr.io/linuxserver/openssh-server:latest
```

Give it a Docker CLI and the compose file the update path expects at
`~/.bioflow/docker-compose.yml`:

```bash
docker exec manual-node apk add --no-cache docker-cli docker-cli-compose
```

```bash
docker exec manual-node chmod 666 /var/run/docker.sock
```

```bash
docker exec -u bioflow -e HOME=/config manual-node sh -c 'mkdir -p ~/.bioflow && printf "services:\n  worker:\n    image: alpine:3.20\n    command: [\"/bin/true\"]\n" > ~/.bioflow/docker-compose.yml'
```

`HOME=/config` matters: `docker exec -u bioflow` does not read the user's passwd
entry, so `~` would otherwise expand to `/root` and the write would fail. The
SSH session the update opens does get `/config`.

Then provision it from the UI at <http://localhost:5273> — Settings → Compute
nodes → Add node, host `manual-node`, port 2222, user `bioflow`, password
`testpw`.

## The checks

### 1. The drain prompt appears only when jobs are running

With **no** jobs running on the node, click Update on its row.

- [ ] No drain prompt appears; the update starts directly.

Then queue work onto the node and, while a job is running, click Update again.

- [ ] The drain prompt appears, and says what it will wait for.

Both directions matter. A prompt that always appears is as wrong as one that
never does — it trains the user to click through it.

### 2. The progress panel advances through its phases

During either update above:

- [ ] The panel shows `pull` → `restart` → `verify` in that order.
- [ ] The percentage advances rather than jumping straight to the end.
- [ ] On the containerized node, the update ends **failed** at `verify` with a
      message naming the worker logs — the sidecar's worker exits immediately,
      so nothing re-enrolls. That is the correct outcome here, and it is the
      same condition
      `test_update_fails_at_verify_when_the_worker_crash_loops` asserts.

### 3. The row's version updates once the worker re-enrolls

**Not observable against the container sidecar** — its stub worker never
enrolls, by design. This one needs a real node running a real BioFlow worker
image that can reach the primary's Mongo and Redis.

- [ ] On a real enrolled node, after a successful update, the row's version
      changes to the new one without a manual page refresh.

## Teardown

```bash
docker rm -f manual-node
```

```bash
./ops/worktree-up.sh --down
```

## Recording the result

Per #474, both checks are pass/fail by observation and there is no test command.
Record what was observed on the issue.
