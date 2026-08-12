# CI runners

`.github/workflows/publish-images.yml` builds the BioFlow container images and
publishes them to GHCR. It runs entirely on **GitHub-hosted runners**.

That was not always true. This repository used to be private and owned by a
personal account, so GitHub's hosted arm64 Linux runners (`ubuntu-24.04-arm`)
were not available on that plan, and the workflow ran on two self-hosted
machines instead. The alternative to a dedicated arm64 runner was QEMU
emulation — which [#46](https://github.com/syntheticgio/bioflow/issues/46)
measured as hours long and liable to fail partway, because
`backend/Dockerfile` compiles bwa-mem2 from source, installs Clair3, and
builds compleasm — or publishing amd64 only and letting arm64 drift, which is
the exact problem [#38](https://github.com/syntheticgio/bioflow/issues/38)
exists to prevent. Making the repository public
([#338](https://github.com/syntheticgio/bioflow/issues/338)) removed the
constraint: hosted `ubuntu-24.04-arm` builds arm64 natively, with no
emulation and no machine to keep online.

## What the workflow uses

| Job | Runner | Builds |
| --- | --- | --- |
| `build` (amd64 leg) | `ubuntu-latest` | `linux/amd64` |
| `build` (arm64 leg) | `ubuntu-24.04-arm` | `linux/arm64` |
| `manifest`, `release`, `version-guard` | `ubuntu-latest` | manifest merge, release notes |

Both `build` legs need a working Docker daemon, which hosted runners provide
out of the box. Nothing else is installed by the workflow —
`docker/setup-buildx-action` brings its own buildx binary.

## No layer cache between runs

Hosted runners are a fresh VM per job, so there is no BuildKit state to
persist the way the old self-hosted machines kept one warm across pushes.
Every run recompiles bwa-mem2, reinstalls Clair3, and rebuilds compleasm from
scratch for the backend image. GitHub Actions cache (`cache-to: type=gha`) is
not a fix for this: the limit is 10GB for the whole repository, and the
backend image alone is about 7.9GB, so it would evict itself before the next
run could reuse it. A cold, slower-but-native hosted build was judged the
better tradeoff over maintaining a persistent machine — see
[#338](https://github.com/syntheticgio/bioflow/issues/338) for the decision.

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
