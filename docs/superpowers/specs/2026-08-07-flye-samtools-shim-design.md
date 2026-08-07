# flye-samtools shim

Fixes [#67](https://github.com/syntheticgio/bioflow/issues/67). Every Flye
assembly on this image dies at the `consensus` stage with
`/bin/sh: 1: flye-samtools: not found`, followed by a parse error on an empty
`consensus.fasta`. No assembly completes, regardless of input.

## Diagnosis

Upstream Flye 2.9.5 vendors its own samtools build and invokes it as
`flye-samtools`. Debian's `flye 2.9.5+dfsg-1` unbundles that binary -- the
`+dfsg` suffix says so -- and declares `Depends: [...] minimap2, samtools`
instead, expecting the packaged `/usr/bin/samtools` to serve.

The unbundling patched one of the two call sites and missed the other.
Verified in the running `biopipe-api-1` container on 2026-08-07:

| File | `SAMTOOLS_BIN` | State |
| --- | --- | --- |
| `flye/polishing/alignment.py:27` | `/usr/bin/samtools` | patched by Debian |
| `flye/utils/sam_parser.py:43` | `flye-samtools` | **missed** |

`sam_parser.py` reaches that constant from three places -- two `depth` calls
(lines 357, 377) and a `view` call (line 442) -- and `dpkg -L flye` confirms
no `flye-samtools` is shipped anywhere in the package. Only `/usr/bin/flye`
and `/usr/bin/flye-modules` exist.

So this is a Debian packaging defect, not a BioFlow one, and not something a
different install route for Flye would avoid without also giving up the
Debian package. The dependency Debian intended is present and correct; only
the name one module asks for is wrong.

## Change

A single `RUN` in `backend/Dockerfile`, immediately after the apt block that
installs `flye` and `samtools` -- both are prerequisites, and keeping the fix
beside the package that needs it is what makes it legible later.

```dockerfile
RUN printf '#!/bin/sh\nexec /usr/bin/samtools "$@"\n' > /usr/local/bin/flye-samtools \
    && chmod +x /usr/local/bin/flye-samtools \
    && flye-samtools --version
```

A wrapper rather than a symlink. Either would function here -- unlike the
`bwa-mem2` case elsewhere in this file, samtools has no argv[0]-relative
dispatch to break -- but the wrapper matches that established precedent and
states the indirection outright instead of leaving it to `ls -l`.

The trailing `flye-samtools --version` is a build-time assertion, deliberately
in the same `RUN` so it cannot drift from the thing it checks. It fails the
build if samtools moves off `/usr/bin/samtools` or the shim is malformed.
This is the same shape as the existing `datasets --version` and
`docker --version` checks in this file.

The block carries a comment recording the diagnosis above: what upstream does,
what Debian changed, and which file was missed. Without it the shim reads as
mystery cruft and gets deleted.

## Verification

The build assertion is the automated coverage, and it is the only automated
coverage there can be. This is a container-image defect: no `pytest` case can
observe it, because a unit test would have to mock the subprocess boundary
that is itself the broken thing. Per CLAUDE.md's note on availability tests,
a test asserting the shim works would pass whether or not the seam were real.

Confirmed by hand in `biopipe-api-1` before writing this spec: with the shim
in place, `flye-samtools --version` reports samtools 1.21 / htslib 1.21, and
both subcommands `sam_parser.py` actually calls (`depth`, `view`) resolve.
That was a throwaway test inside a running container and does not survive a
rebuild -- it establishes the fix is correct, not that it is installed.

Full verification is a real Flye assembly running past `consensus` on a
rebuilt image.

## Out of scope

- **No runtime probe in `tools.flye()`.** It would guard a state that cannot
  occur: once the shim is baked in, the build either produced it or failed.
  A probe that can never fire is dead weight.
- **No `suggestion_service.py` change.** Flye is already registered and
  suggestible; this only makes it finish.
