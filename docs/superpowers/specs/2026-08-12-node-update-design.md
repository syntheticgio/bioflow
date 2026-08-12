# Updating compute nodes from Node Settings

Date: 2026-08-12
Status: Design
Issue: [#240](https://github.com/syntheticgio/bioflow/issues/240)

## Problem

When a new backend image is published, every compute node keeps running
whatever image it started with. There is no way to see that a node is behind,
and no way to update it short of SSH-ing to the machine by hand and re-running
`docker pull` and `docker compose up -d`.

Two distinct gaps underlie this:

1. **Nothing reports a node's version.** The worker heartbeat
   (`worker.py:_register_worker`) publishes `last_seen`, `slots`, `running`,
   `draining`, and `node_id`. It does not publish what image the worker is
   running, so the primary cannot tell a current node from a stale one.
2. **Provisioning credentials are not retained.** `ProvisionRequest` accepts a
   password or private key, uses it once, and drops it. `NodeProvisionTask`
   records `host` and `node_name` but no `username` or `port`. After
   provisioning, the primary has no route back into the machine.

The provisioning flow already performs an update in all but name: phases 5 and
6 of `_provision_node` are `docker pull` followed by `docker compose up -d`.
What is missing is the ability to run those two phases again later, and a
reason to know they are needed.

## Goal

Node Settings shows the version each node is running, flags nodes running an
image older than the primary's, and offers an Update button that pulls the
current image and restarts the worker over SSH — reporting progress the same
way provisioning does, and confirming success by observing the updated worker
re-enroll.

## Architecture

```
┌───────────────────────────┐       SSH (managed key)   ┌──────────────────────┐
│  Primary (web UI + API)   │ ─────────────────────────▶│  Child machine       │
│                           │                           │                      │
│  Node Settings table      │  1. connect (stored key)  │  Docker daemon       │
│   · version per node      │  2. drain (optional)      │        │             │
│   · "Update" when stale   │  3. docker pull           │        ▼             │
│        │                  │  4. compose up -d         │  ┌─────────────┐     │
│        ▼                  │                           │  │ worker      │────▶│ enrolls
│  POST /nodes/{id}/update  │                           │  └─────────────┘     │  with
│        │                  │                           │        │             │  primary
│        ▼                  │  progress via             │        ▼             │
│  Background task ─────────┤  GET /nodes/update/{tid}  │  heartbeat carries   │
│        │                  │                           │  image_digest        │
│        ▼                  │                           │        │             │
│  verify: digest matches ◀─┼───────────────────────────┼────────┘             │
└───────────────────────────┘                           └──────────────────────┘
```

The update executor is phases 5–6 of `_provision_node`, extracted so both the
provisioning flow and the update flow call the same code.

Authentication uses a **managed keypair** generated during provisioning, not
the credential the user typed. See [Managed key](#managed-key).

## Scope

**In scope:**

- Worker reports its image digest and version in the heartbeat
- Node Settings shows each node's version and flags stale nodes
- A managed SSH keypair generated and installed during provisioning
- `POST /nodes/{node_id}/update` running pull + restart over SSH
- Click-time choice between draining running jobs and swapping immediately
- Progress polling, mirroring the provisioning flow

**Out of scope:**

- Updating nodes that were not provisioned from the BioFlow UI. These can
  report a version but have no stored key; the button renders disabled.
- Automatic or scheduled updates. The button is manual.
- Rolling back to a previous image. See [D6](#d6-no-automatic-rollback).
- Updating the primary itself, which the launcher already handles
  (`launcher/src/update-logic.ts`).

## Requirements

### Version reporting

- **NU-1** The worker includes the digest of the image it is running in the
  payload it writes to the `WORKERS` Redis hash on each heartbeat.
- **NU-2** The worker includes its `app.version.__version__` string in that
  same payload.
- **NU-3** A worker that cannot determine its own image digest heartbeats with
  a null digest rather than failing to heartbeat.
- **NU-4** `enumerate_nodes()` returns each node's reported digest and version.
- **NU-5** The primary persists the last reported digest and version on the
  `Node` document, so a node that is offline still shows the version it last
  ran.
- **NU-6** Node Settings displays the version of each node in the node table.
- **NU-7** Node Settings marks a node as stale when its reported digest differs
  from the digest the primary is running.
- **NU-8** Node Settings displays "Unknown" as the version of a node reporting
  a null digest, and does not mark that node stale.

### Managed key

- **NU-9** During provisioning, the primary generates an Ed25519 keypair
  dedicated to that node.
- **NU-10** The primary appends the public half of that keypair to the node
  user's `~/.ssh/authorized_keys`, preserving any keys already present.
- **NU-11** The primary verifies the installed key by opening a second SSH
  connection authenticated with the private half and running a command.
- **NU-12** The primary stores the private half encrypted with
  `app.services.ai.crypto.encrypt`.
- **NU-13** The primary stores the node's SSH host, port, and username
  alongside the encrypted key.
- **NU-14** Provisioning fails, and no image is pulled, when the keypair cannot
  be installed or the verification connection does not authenticate.
- **NU-15** Provisioning does not store the password or private key supplied in
  the provisioning request.
- **NU-16** The provisioning form states that BioFlow will install a
  dedicated SSH key on the node and retain it to perform updates.

### Update

- **NU-17** `POST /nodes/{node_id}/update` starts an update and returns a task
  id for polling.
- **NU-18** The endpoint rejects with 409 a node that has no stored managed
  key.
- **NU-19** The endpoint rejects with 409 a node that already has an update in
  progress.
- **NU-20** The endpoint attempts an update on a node with no online workers,
  and reports failure naming the machine as unreachable only when the SSH
  connection itself fails.
- **NU-21** The request carries whether to drain running jobs first or swap
  immediately.
- **NU-22** When draining, the primary stops the node's worker with SIGTERM and
  waits for running jobs to finish before restarting it. Draining a node with
  no running jobs completes immediately.
- **NU-23** The update pulls the new image before stopping the running worker,
  so a failed pull leaves the node running its current image.
- **NU-24** An update whose `docker pull` fails reports failure and names the
  pull as the failing step.
- **NU-25** An update reports success only after a worker on that node
  heartbeats with the digest that was pulled.
- **NU-26** An update where no worker reports the new digest within 120 seconds
  reports failure naming the timeout.
- **NU-27** `GET /nodes/update/{task_id}` returns the status, phase, message,
  and error of an update task.
- **NU-28** Update tasks left running when the API restarts are marked failed
  on the next startup.

### Interface

- **NU-29** Node Settings shows an Update control on each node marked stale and
  on each node with a stored managed key reporting no version, so a node whose
  worker is down can still be updated.
- **NU-30** The Update control is disabled, with the reason shown, on a node
  with no stored managed key.
- **NU-31** Clicking Update on a node with at least one running job presents a
  choice between draining and updating immediately, with draining preselected.
- **NU-32** Node Settings polls and displays update progress while an update
  runs.

## Data model

### `Node` (`backend/app/models/node.py`)

Additive; every field is nullable or defaulted, so existing documents load
unchanged.

```python
# How to reach this node over SSH for updates. Null on nodes that enrolled
# themselves rather than being provisioned from the UI -- those report a
# version but cannot be updated from here.
ssh_host: str | None = None
ssh_port: int = 22
ssh_username: str | None = None
ssh_key_enc: bytes | None = None          # Fernet; managed key's private half
ssh_key_installed_at: datetime | None = None

# What this node is running, from the worker heartbeat.
image_digest: str | None = None
version: str | None = None
```

`ssh_key_enc` being null is load-bearing rather than merely empty: it is the
difference between a node provisioned from the UI (updatable) and one brought
up by hand with `docker-compose.child-node.yml` (not updatable). NU-30 renders
that difference rather than offering a button that cannot work.

Version fields are persisted here, not only in the Redis heartbeat, because
Redis entries expire with the worker. The last-known version of an offline node
is exactly what someone deciding whether to bring it up wants to see (NU-5).

### `NodeUpdateTask` (new)

Mirrors `NodeProvisionTask` field for field: `task_id`, `status`, `phase`,
`message`, `pct`, `node_name`, `host`, `started_at`, `finished_at`, `error`.
Collection `node_updates`.

Reusing `NodeProvisionTask` was considered and rejected: the phase vocabularies
differ, and a shared collection would make "when was this node last updated"
filter on a discriminator forever. The two documents share a progress-reporting
helper rather than a collection.

### Heartbeat payload (`worker.py:_register_worker`)

Gains `image_digest` and `version` beside the existing keys. The digest is read
once at worker startup, not per heartbeat — it cannot change while the process
lives.

Reading it from inside the container uses the mounted Docker socket to inspect
the worker's own container. The child node already mounts the socket, and the
backend image already ships a `docker` client
(`app/queue/tool_handlers.py:_docker_client`), so this needs no new capability.
Where it fails, the digest is null and NU-3 and NU-8 govern.

## Data flow

### Provisioning, with the new phase

```
validate_ssh → verify_docker → setup_install → write_env
    → install_key                                   ← new
    → pull_image → start_worker → enrolled
```

`install_key`:

1. `asyncssh.generate_private_key("ssh-ed25519")`, comment
   `bioflow-node-<node_name>`.
2. `mkdir -p ~/.ssh && chmod 700 ~/.ssh`; append the public half to
   `authorized_keys`; `chmod 600`. **Append, never overwrite** (NU-10) —
   clobbering a user's existing keys is catastrophic and trivially avoided.
3. Open a **second** connection authenticating with the new private key and run
   a trivial command (NU-11).
4. Only on a verified round trip: encrypt and store (NU-12, NU-13).

Step 3 is what makes NU-14 meaningful. Without it, success means the append
command exited 0, which is not the same as the key working: `authorized_keys`
can be written successfully and still be ignored because of directory
permissions, an `AuthorizedKeysFile` pointing elsewhere, or
`PubkeyAuthentication no`.

### Update

```
POST /nodes/{node_id}/update
    → 409: no stored key (NU-18) / already updating (NU-19)
    → task_id, then in the background:

    connect → pull_image → [drain] → restart → verify → done
```

`verify` polls for a worker on the node heartbeating with the pulled digest
(NU-25), timing out at 120 seconds (NU-26). Without it, "success" would mean
`docker compose up -d` exited 0 — true even when the new container immediately
crash-loops, which is the exact failure this feature exists to fix.

Draining relies on machinery that already exists: on SIGTERM the worker stops
claiming, waits up to `settings.drain_timeout_seconds` for running jobs, then
requeues whatever remains *without* counting a failed attempt
(`worker.py:_drain`). It already publishes `draining: true` in the heartbeat,
so the primary can observe progress. Draining is therefore
`docker compose stop` plus watching, not new worker code.

The frontend polls `GET /nodes/update/{task_id}` on the 3-second interval
`SettingsNodes.tsx` already uses for provisioning.

## Error handling

| Failure | Behavior |
|---|---|
| `authorized_keys` not writable, or key does not authenticate | Provisioning fails at `install_key`, before any pull. No fallback to storing the user's credential. |
| No `ssh_key_enc` on the node | 409; button disabled with the reason shown. |
| Stored key rejected (rotated, `authorized_keys` cleaned) | Task fails: "The managed key was rejected. Re-provision this node." |
| Node's worker is down but the machine is reachable | Update proceeds. This is the case D1 chose SSH for; drain is a no-op with nothing running. |
| Machine itself unreachable | Task fails at connect, naming the machine as unreachable (NU-20). |
| Drain times out | Proceeds to the swap; jobs requeue on SIGTERM regardless. Message names how many were requeued. |
| `docker pull` fails | Task fails; node still runs its current image (NU-23). |
| New container crash-loops | Task fails at `verify` (NU-26). Old image is not restored. |
| Primary restarts mid-update | Marked failed by the startup sweep (NU-28), as `_clean_orphaned_provisions` already does for provisioning. |

## Decisions

### D1: Push over SSH rather than node-initiated pull

A node-initiated design — the worker notices a newer image and updates itself —
needs no stored credentials and scales without the primary sequencing anything.
It was rejected because its failure mode defeats the feature's purpose: a node
that is crash-looping or misconfigured cannot update itself, and that is
precisely when the update button is wanted. SSH is an out-of-band channel that
still reaches a broken node.

The self-update mechanics are also awkward: a container cannot replace itself.
`restart: unless-stopped` plus exit restarts the *existing* container on the
*old* image, so the pulled image goes unused. Doing it properly requires
spawning a detached sibling container to perform the swap after the worker
exits — workable, and the same pattern `variant_runner.py` uses for
DeepVariant, but it puts an unobserved swap in the least testable corner of the
system.

### D2: A dedicated keypair rather than the user's credential

Storing whichever credential the user supplied at provisioning time is simpler
and was rejected on two counts. A password is the credential most likely to be
rotated, which would break updates silently months later; and it is the user's
own login credential rather than one scoped to this purpose.

A generated keypair costs one extra provisioning step, is revocable per node
without touching the user's credentials, survives a password change, and means
the user types their password exactly once.

### D3: Fail loudly when the key cannot be installed

Falling back to storing the supplied credential when `authorized_keys` is not
writable was rejected: it makes the security property of D2 depend on a
condition nobody observed at the time. A node that cannot take a managed key
fails provisioning with the reason stated.

### D4: Reuse the existing Fernet module

`app/services/ai/crypto.py` already encrypts AI provider API keys, with the key
file beside the database under `.biopipe/`. Reusing it introduces no new
key-management decision.

Its own docstring is honest about the scope, and that honesty transfers: the
key file sits on the same disk as the Mongo data, so anyone with shell access
to the primary has both. What it defends against is a look at the collection —
an opened Compass window, a stray `mongodump` in a backup.

An SSH private key is a stronger secret than an API key: it grants shell on
another machine. The same encryption is defensible for a single-user LAN tool,
but it is a real escalation of what a compromised primary means, which is why
NU-16 requires the provisioning form to say what is being installed and
retained rather than leaving the user to discover it.

### D5: Pull before stop

Downloading the image while the current worker keeps running means a failed
pull costs nothing (NU-23). The node is only stopped once the new image is on
disk.

### D6: No automatic rollback

Re-pinning to a previous digest is a real operation with its own failure modes.
Performing it automatically, on a machine nobody is watching, risks converting
one broken node into an oscillating one. A failed update reports that the node
is down and needs attention, which is honest and actionable.

### D7: Ask about running jobs, defaulting to drain

Draining unconditionally would block an update behind a six-hour alignment.
Swapping unconditionally would kill running work, and while the queue requeues
it without penalty, that is the user's call on a long job rather than a default
worth imposing. NU-31 asks, preselecting the safe option.

## Testing

Backend tests follow the seam `backend/tests/api/test_node_provision.py`
already uses: patch `asyncssh` at the module level.

- **`install_key`** — generates a valid key; appends rather than overwrites
  (NU-10); fails when the verification connection is rejected (NU-11, NU-14);
  stores nothing on verification failure; stores ciphertext, not plaintext
  (NU-12); does not retain the supplied credential (NU-15).
- **Update endpoint** — 409 without a stored key (NU-18); 409 while an update
  runs (NU-19); proceeds on a node whose worker is down, and reports the
  machine unreachable when SSH itself fails (NU-20); honors the drain choice
  (NU-21).
- **Update executor** — a failed pull leaves the node untouched (NU-23, NU-24);
  success requires a matching digest, not exit 0 (NU-25); timeout reports
  failure (NU-26); the startup sweep marks orphans failed (NU-28).
- **Version reporting** — digest reaches `enumerate_nodes()` (NU-4); persists to
  the `Node` document (NU-5); a null digest degrades rather than raising
  (NU-3, NU-8).

Per `CLAUDE.md`, UI verification is manual at localhost:5173 — this repo has no
jsdom setup and no `.test.tsx` files. The staleness comparison (NU-7, NU-8)
therefore lives in a pure module and is unit-tested there, the way
`launcher/src/update-logic.ts` isolates the launcher's equivalent logic.

**What tests cannot cover.** Every test mocks `asyncssh`, so the real
`authorized_keys` round trip against a real sshd is never exercised: an error
in the remote shell commands would pass green. One manual provision against a
real machine is required before this ships, and it is the only way NU-10 and
NU-11 are genuinely verified.
