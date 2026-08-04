# GHCR Image Publishing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish the BioFlow container images to GitHub Container Registry and convert `docker-compose.yml` from build contexts to image references, so a machine with no source tree can start the stack.

**Architecture:** Two images, not three. `api` and `worker` share `backend/Dockerfile` and differ only by `command:`, so they become one published image (`bioflow-backend`) referenced twice. The frontend's `prod` target becomes `bioflow-web`. The `build:` directives move to `docker-compose.override.yml`, which always loads locally and never ships, so local development keeps building from source with the same command it uses today.

**Tech Stack:** Docker Buildx, GitHub Container Registry, Docker Compose.

**Issue:** [#37](https://github.com/syntheticgio/bioflow/issues/37). Prerequisite for [#28](https://github.com/syntheticgio/bioflow/issues/28).

---

## Before you start

**Run everything in this plan from the main checkout, not a worktree.** A `PreToolUse` hook blocks bare `docker compose` in a worktree, and for good reason: Compose resolves the relative bind mounts in `docker-compose.override.yml` against the invoking directory while the project name stays pinned to `biopipe`, so running it from a worktree silently repoints the one real stack at that worktree's source. Find the main checkout with `git rev-parse --show-toplevel` from a non-worktree shell.

**This plan rebuilds and restarts the stack the user actually runs.** Task 6 takes the stack down and brings it back up. Do not run it while a pipeline job is in flight — check the queue is idle first.

**Architecture scope.** This publishes `linux/arm64` only. The development machine is Apple Silicon, and building `linux/amd64` here means QEMU emulation of a Dockerfile that compiles bwa-mem2 from source, installs Clair3, and builds compleasm — hours of emulated compilation, liable to fail partway. Issue [#46](https://github.com/syntheticgio/bioflow/issues/46) attaches an amd64 runner and publishes that architecture natively. Until then the launcher runs on Apple Silicon only. Do not attempt the amd64 build here.

**Work on a branch.** Tasks 4 and 5 change files; create a branch before Task 4 so Task 8's merge has something to merge:

```bash
cd "$(git rev-parse --show-toplevel)" && git checkout -b ghcr-image-publishing
```

Tasks 1-3 touch no files and can run from wherever you are.

**Sizes to expect.** The backend image is 7.89GB locally and the web image 659MB. Compressed layers push smaller than that, but the first push still moves several gigabytes. Budget time and bandwidth; a failed push mid-upload resumes on retry rather than starting over.

---

## Task 1: Grant the token package-write access

The `gh` token currently carries `admin:public_key, gist, read:org, repo`. GHCR pushes need `write:packages`, and the failure without it is a `denied` at the end of a multi-gigabyte upload rather than at login.

**Files:** none — this is machine setup.

- [ ] **Step 1: Confirm the scope is missing**

```bash
gh auth status 2>&1 | grep -i "token scopes"
```

Expected: a list that does *not* include `write:packages`.

- [ ] **Step 2: Add the scope**

```bash
gh auth refresh -h github.com -s write:packages -s read:packages
```

This opens a browser for confirmation. If the environment cannot open one, it prints a one-time code and URL to use manually.

- [ ] **Step 3: Verify**

```bash
gh auth status 2>&1 | grep -i "token scopes"
```

Expected: the list now includes `write:packages`.

- [ ] **Step 4: Log Docker in to GHCR**

```bash
gh auth token | docker login ghcr.io -u syntheticgio --password-stdin
```

Expected: `Login Succeeded`.

---

## Task 2: Build and push the backend image

**Files:** none — this task produces registry artifacts.

- [ ] **Step 1: Choose the version tag**

Read the current backend version so the published tag matches the code:

```bash
cd "$(git rev-parse --show-toplevel)" && grep -m1 "^version" backend/pyproject.toml
```

Expected: `version = "0.1.0"` as of this writing. Use that value wherever `<VERSION>` appears below, so the two tag pairs are `0.1.0` and `latest`.

The `^` anchor matters: `pyproject.toml` also contains `target-version = "py312"` for ruff, and an unanchored match can pick that up depending on ordering.

- [ ] **Step 2: Build for arm64 and push**

From the repository root:

```bash
docker buildx build --platform linux/arm64 --tag ghcr.io/syntheticgio/bioflow-backend:<VERSION> --tag ghcr.io/syntheticgio/bioflow-backend:latest --push ./backend
```

Expected: a long build (the bwa-mem2 arm64 source compile and Clair3 install dominate), then a push of several gigabytes. Most layers come from the local cache if the stack was built recently.

- [ ] **Step 3: Verify the image is in the registry**

```bash
docker manifest inspect ghcr.io/syntheticgio/bioflow-backend:latest | grep -A2 platform
```

Expected: `"architecture": "arm64"`, `"os": "linux"`.

- [ ] **Step 4: Verify the tools survived the round trip**

The image is only useful if its bioinformatics tools work. Pull it fresh and probe a representative sample — one Debian package, one source-built binary, one script-installed tool:

```bash
docker run --rm ghcr.io/syntheticgio/bioflow-backend:latest sh -c "fastp --version; bwa-mem2 version; run_clair3.sh --version; compleasm --version; datasets --version"
```

Expected: each prints a version. `bwa-mem2` is the one to watch — it is the arm64 source build, and a broken build reports nothing or fails to exec.

---

## Task 3: Build and push the web image

**Files:** none.

- [ ] **Step 1: Build the prod target and push**

The frontend Dockerfile has three stages; only `prod` is published. `dev` exists for the override file and must not be shipped.

```bash
docker buildx build --platform linux/arm64 --target prod --tag ghcr.io/syntheticgio/bioflow-web:<VERSION> --tag ghcr.io/syntheticgio/bioflow-web:latest --push ./frontend
```

Expected: a much faster build than the backend, then a push of roughly 660MB.

- [ ] **Step 2: Verify it serves the built app**

```bash
docker run --rm -d --name bioflow-web-check -p 18080:80 ghcr.io/syntheticgio/bioflow-web:latest && sleep 3 && curl -s -o /dev/null -w "%{http_code}\n" http://localhost:18080/ && docker stop bioflow-web-check
```

Expected: `200`. A `404` means the `--target prod` flag was omitted and the wrong stage was published.

- [ ] **Step 3: Make both packages public**

New GHCR packages default to private, and the launcher's users are anonymous.

Visit `https://github.com/users/syntheticgio/packages`, open each of `bioflow-backend` and `bioflow-web`, then Package settings → Change visibility → Public.

- [ ] **Step 4: Verify anonymous pull works**

This is the check that matters — it fails if visibility is still private, and it is exactly what an end user's machine does:

```bash
docker logout ghcr.io && docker manifest inspect ghcr.io/syntheticgio/bioflow-web:latest > /dev/null && echo "anonymous pull OK"
```

Expected: `anonymous pull OK`. Log back in afterwards with the Task 1 Step 4 command if you need to push again.

---

## Task 4: Convert the compose file to image references

This is the change that lets a machine with no source tree start the stack, and the one that breaks local builds if the override half is forgotten.

**Files:**
- Modify: `docker-compose.yml`
- Modify: `docker-compose.override.yml`

- [ ] **Step 1: Point the three services at images**

In `docker-compose.yml`, replace the `api` service's build block:

```yaml
  api:
    build:
      context: ./backend
```

with:

```yaml
  api:
    # api and worker are the same image with different commands -- one build
    # context, one published image, referenced twice. Building them separately
    # would push ~8GB of identical layers twice.
    image: ghcr.io/syntheticgio/bioflow-backend:${BIOFLOW_TAG:-latest}
```

Replace the `worker` service's build block:

```yaml
  worker:
    build:
      context: ./backend
```

with:

```yaml
  worker:
    image: ghcr.io/syntheticgio/bioflow-backend:${BIOFLOW_TAG:-latest}
```

Replace the `web` service's build block:

```yaml
  web:
    build:
      context: ./frontend
      target: prod
```

with:

```yaml
  web:
    image: ghcr.io/syntheticgio/bioflow-web:${BIOFLOW_TAG:-latest}
```

- [ ] **Step 2: Move the build directives to the override**

In `docker-compose.override.yml`, add a `build:` block to each of the three services alongside what is already there. The `api` service becomes:

```yaml
  api:
    # The base file names a published image so an end user's machine can pull
    # it. Local development builds from source instead, and this is what makes
    # `docker compose up -d --build` still do that -- the override always
    # loads, and it is now the only place that knows how to build.
    build:
      context: ./backend
    command: ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
```

Add to the `worker` service, above its existing `volumes:` key:

```yaml
  worker:
    build:
      context: ./backend
```

And to the `web` service, replacing its existing `build:` block (which currently sets only `target`):

```yaml
  web:
    build:
      context: ./frontend
      target: dev
```

- [ ] **Step 3: Verify the local build path still works**

From the main checkout root:

```bash
docker compose config --services && docker compose build api 2>&1 | tail -5
```

Expected: the service list prints, and `api` builds from source rather than reporting a missing build context.

- [ ] **Step 4: Verify the shipped path resolves to images**

This is what the launcher will run — base file only, no override:

```bash
docker compose -f docker-compose.yml config | grep -E "^\s+image:"
```

Expected: five image lines — mongo, redis, and the three BioFlow services pointing at `ghcr.io/syntheticgio/*`. No `build:` keys appear anywhere in that output.

- [ ] **Step 5: Commit**

```bash
git add docker-compose.yml docker-compose.override.yml
git commit -m "feat: publish images to GHCR and reference them from compose"
```

---

## Task 5: Add the launcher's port and bind variables

The launcher sets the port and the bind address through `.env`. Both are hardcoded today, and the bind change tightens a real exposure.

**Files:**
- Modify: `docker-compose.yml`
- Modify: `.env.example`

- [ ] **Step 1: Parameterize the published ports**

In `docker-compose.yml`, replace the `api` service's ports block:

```yaml
    ports:
      - "8000:8000"
```

with:

```yaml
    ports:
      # Loopback by default. BioFlow has no authentication, so binding every
      # interface exposes it to anyone on the network -- which is what
      # "8000:8000" did. The launcher's "allow access from other devices"
      # toggle sets BIND_ADDRESS=0.0.0.0 when the user asks for it.
      - "${BIND_ADDRESS:-127.0.0.1}:${API_PORT:-8000}:8000"
```

Replace the `web` service's ports block:

```yaml
    ports:
      - "5173:80"
```

with:

```yaml
    ports:
      - "${BIND_ADDRESS:-127.0.0.1}:${WEB_PORT:-5173}:80"
```

- [ ] **Step 2: Document the variables**

Add to `.env.example`, after the `BIOINFO_HOME` block:

```bash
# Published ports and the interface they bind to. The native launcher writes
# these; they are here so a manual setup can change them too.
# BIND_ADDRESS=0.0.0.0 exposes BioFlow to other devices on the network, which
# is off by default because BioFlow has no authentication.
WEB_PORT=5173
API_PORT=8000
BIND_ADDRESS=127.0.0.1

# Which published image tag the stack runs. Ignored for local builds, which
# use the build directives in docker-compose.override.yml.
BIOFLOW_TAG=latest
```

- [ ] **Step 3: Verify the default is loopback**

```bash
docker compose config | grep -A4 "published"
```

Expected: `host_ip: 127.0.0.1` on the published port entries.

- [ ] **Step 4: Verify the toggle opens it**

```bash
BIND_ADDRESS=0.0.0.0 docker compose config | grep -A4 "published" | grep host_ip
```

Expected: `host_ip: 0.0.0.0`.

- [ ] **Step 5: Commit**

```bash
git add docker-compose.yml .env.example
git commit -m "feat: parameterize published ports and bind address"
```

---

## Task 6: Verify the running stack still works

The stack the user actually runs must come back healthy on the changed compose files. Do this from the main checkout with the queue idle.

- [ ] **Step 1: Rebuild and restart**

```bash
docker compose up -d --build api web worker
```

Expected: all three build from source (via the override) and start. If any reports a missing build context, Task 4 Step 2 was incomplete.

- [ ] **Step 2: Confirm the stack is serving**

```bash
curl -s -o /dev/null -w "web %{http_code}\n" http://localhost:5173/ && curl -s -o /dev/null -w "api %{http_code}\n" http://localhost:8000/healthz
```

Expected: `web 200` and `api 200`.

- [ ] **Step 3: Confirm the stack is on the main checkout, not a worktree**

```bash
docker inspect biopipe-worker-1 --format '{{range .Mounts}}{{.Source}}{{"\n"}}{{end}}' | grep -v "^$"
```

Expected: paths under the main checkout. Anything under `.claude/worktrees/` means the stack was started from the wrong directory — rerun Step 1 from the main checkout root.

- [ ] **Step 4: Run the backend suite**

```bash
docker compose exec api python -m pytest tests/ -q 2>&1 | tail -5
```

Expected: all tests pass. Read the count, not just the exit code. This command is correct only from the main checkout; from a worktree it silently tests main's code.

- [ ] **Step 5: Confirm loopback binding took effect**

```bash
docker inspect biopipe-web-1 --format '{{json .NetworkSettings.Ports}}'
```

Expected: `"HostIp":"127.0.0.1"` for port 80.

Note this is a real behavior change for local use: the stack is no longer reachable from other devices on the network unless `BIND_ADDRESS=0.0.0.0` is set in `.env`. If the user has been reaching BioFlow from a phone or another machine, that stops working until they set it — mention this when reporting the task complete.

- [ ] **Step 6: Verify a pipeline job still runs**

Image changes that break a tool do not show up in the API healthcheck. Restart the worker so it reloads handlers, then run one QC job through the UI at localhost:5173 against an existing project and confirm it completes.

```bash
docker compose restart worker
```

- [ ] **Step 7: Commit nothing, or fix and commit**

This task changes no files. If it exposed a problem, fix it, rerun the suite, and commit the fix on its own.

---

## Task 7: Verify a source-free install actually works

Everything up to here proves the compose file *parses* as image references. This proves the thing the issue exists for: a machine with no source tree can start BioFlow. Skipping it would leave the central claim untested.

- [ ] **Step 1: Build a source-free install directory**

```bash
mkdir -p /tmp/bioflow-nosrc && cd "$(git rev-parse --show-toplevel)" && cp docker-compose.yml /tmp/bioflow-nosrc/ && ls /tmp/bioflow-nosrc/
```

Expected: `docker-compose.yml` alone. No `backend/`, no `frontend/`, no override file — exactly what the launcher writes.

- [ ] **Step 2: Write an `.env` like the launcher's**

```bash
printf 'BIOINFO_HOME=/tmp/bioflow-nosrc-data\nWEB_PORT=5273\nAPI_PORT=8100\nBIND_ADDRESS=127.0.0.1\nBIOFLOW_TAG=latest\n' > /tmp/bioflow-nosrc/.env && mkdir -p /tmp/bioflow-nosrc-data && cat /tmp/bioflow-nosrc/.env
```

Ports differ from the main stack's so both can run at once.

- [ ] **Step 3: Start it under its own project name**

The distinct project name is what keeps this from colliding with the real stack — the same mechanism `ops/worktree-up.sh` uses.

```bash
docker compose --project-directory /tmp/bioflow-nosrc --project-name bioflow-nosrc up -d
```

Expected: images are pulled from ghcr.io if not cached, then all five services start. A "context path does not exist" error means the base compose file still has a `build:` key.

- [ ] **Step 4: Confirm it serves**

```bash
sleep 20 && curl -s -o /dev/null -w "web %{http_code}\n" http://localhost:5273/ && curl -s -o /dev/null -w "api %{http_code}\n" http://localhost:8100/healthz
```

Expected: `web 200` and `api 200`. This is the proof that the launcher's approach works.

- [ ] **Step 5: Tear it down**

```bash
docker compose --project-directory /tmp/bioflow-nosrc --project-name bioflow-nosrc down -v && rm -rf /tmp/bioflow-nosrc /tmp/bioflow-nosrc-data
```

Expected: containers and volumes removed. `-v` matters — this test created its own mongo and redis volumes.

- [ ] **Step 6: Confirm the main stack is unaffected**

```bash
curl -s -o /dev/null -w "main web %{http_code}\n" http://localhost:5173/
```

Expected: `200`. The project-name isolation should have kept the real stack untouched throughout.

---

## Task 8: Close out

- [ ] **Step 1: Record what shipped on the issue**

Comment on [#37](https://github.com/syntheticgio/bioflow/issues/37) with the published tags, the verified digests, and the result of the Task 7 source-free run. Note explicitly that only `linux/arm64` was published and that #46 covers amd64.

- [ ] **Step 2: Unblock the launcher issue**

Comment on [#28](https://github.com/syntheticgio/bioflow/issues/28) that its image prerequisite is satisfied for arm64, so Tasks 1–15 of the launcher plan can proceed and Task 16's verification is possible on Apple Silicon.

- [ ] **Step 3: Update `docs/TODO.md` if it names this work**

Check whether any open entry describes the build-context problem. If one does and this closed it, append ` — FIXED` with a note saying what shipped and where, and move the entry to `docs/TODO-done.md` per `CLAUDE.md`. If none does, change nothing.

```bash
grep -n -i "image\|registry\|ghcr\|build context" docs/TODO.md
```

- [ ] **Step 4: Merge and push**

The suite passed in Task 6 and the source-free install worked in Task 7, which is the bar for merging here.

```bash
git checkout main && git merge --no-ff ghcr-image-publishing && docker compose exec api python -m pytest tests/ -q 2>&1 | tail -3 && git push origin main
```

Re-running the suite after the merge is deliberate: `main` may have moved, and a green result from before the merge does not describe the merged tree.

---

## Verification Summary

| Claim | Proved by |
|---|---|
| Images exist and are arm64 | Task 2 Step 3, Task 3 Step 1 |
| The tools inside still work | Task 2 Step 4 |
| The web image serves the built app | Task 3 Step 2 |
| Anonymous users can pull | Task 3 Step 4 |
| Local builds still build from source | Task 4 Step 3, Task 6 Step 1 |
| The shipped file has no build contexts | Task 4 Step 4 |
| Ports bind to loopback by default | Task 5 Step 3, Task 6 Step 5 |
| The real stack still passes its suite | Task 6 Step 4 |
| A pipeline job still runs | Task 6 Step 6 |
| **A machine with no source tree can start BioFlow** | **Task 7 Step 4** |
