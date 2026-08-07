#!/bin/sh
# Build winnowmap from source. Winnowmap ships NO binary releases -- v2.03's
# GitHub release asset list is empty -- so unlike meryl this is a compile,
# the shape that already bit bwa-mem2 and compleasm in this repo. Verified
# end to end on this machine's aarch64 host, and the two gotchas below are
# in neither Winnowmap's README nor its Makefile comments.
#
# (a) The documented `make arm_neon=1 aarch64=1` DOES NOT WORK on aarch64.
# src/Makefile's aarch64 branch appends `-D_FILE_OFFSET_BITS=64
# -fsigned-char` to CPPFLAGS -- but the top-level Makefile does
# `export CPPFLAGS=...` and then `$(MAKE) -e -C src`. Under `-e`, the
# exported environment value OVERRIDES the sub-make's `CPPFLAGS+=`, so the
# aarch64 branch's flags are computed and then silently discarded. `char`
# is unsigned on ARM, and the build dies at chain.c:10 with:
#
#   chain.c:10:9: error: narrowing conversion of '-1' from 'int' to 'char'
#
# The fix is to pass the *complete* flag string as a make variable, which
# beats `-e`, rather than relying on the aarch64=1 switch to append anything.
#
# (b) Build ONLY the `winnowmap` target, not the top-level `all` / `winnowmap`
# recipe's `ext/meryl` step. After bin/winnowmap links successfully, the
# Makefile continues into a bundled, ancient vendored meryl that fails on
# Debian trixie:
#
#   utility/src/utility/system.C:37:10: fatal error: sys/sysctl.h: No such
#   file or directory
#
# sys/sysctl.h was removed from glibc. This is not a problem to solve: it is
# a second, older copy of the tool install-meryl.sh already installs at
# 1.4.2 (see that script). So this script builds `src` directly and links
# the winnowmap binary itself, bypassing the top-level Makefile's `all`
# target entirely rather than patching around its meryl step.
#
# Runtime deps, verified via `ldd bin/winnowmap`: libz, libstdc++, libm,
# libgomp, libgcc_s, libc -- all already present in this image. Unlike
# meryl, winnowmap needs NO vendored OpenSSL 1.1 and no LD_LIBRARY_PATH
# entry.
#
# License: GitHub reports license.spdx_id "NOASSERTION" for this repo,
# which is GitHub failing to classify the file, not an absent license.
# Verified 2026-08-07 via `gh api repos/marbl/Winnowmap/contents/LICENSE`:
# an NIH/NHGRI public-domain dedication, noting the codebase is a joint
# work whose individual contributions may carry their own licenses per
# source file. See tools.py's TOOL_META["winnowmap"] entry.

set -eu

WINNOWMAP_VERSION="${WINNOWMAP_VERSION:-2.03}"
INSTALL_DIR="/opt/winnowmap"
BUILD_DIR="/tmp/winnowmap-build"

apt-get update
apt-get install -y --no-install-recommends git build-essential zlib1g-dev ca-certificates

echo "Fetching Winnowmap v${WINNOWMAP_VERSION}..."
rm -rf "${BUILD_DIR}"
git clone --depth 1 --branch "v${WINNOWMAP_VERSION}" \
    https://github.com/marbl/Winnowmap.git "${BUILD_DIR}"

# The complete aarch64 CPPFLAGS, computed by hand from src/Makefile's own
# base flags plus its aarch64 branch's additions -- passed as a SINGLE make
# variable argument so it survives the top-level Makefile's `-e`, which is
# exactly what makes gotcha (a) above happen when arm_neon=1/aarch64=1 are
# used instead. Built with `set --` rather than an unquoted variable: the
# CPPFLAGS value itself contains spaces, and passing it unquoted (or via
# word-splitting a single string) hands `make` a pile of separate
# arguments -- `-Wall`, `-O2`, etc -- that `make` reads as its OWN options
# rather than as part of the CPPFLAGS value, and fails immediately with
# "make: invalid option -- 'W'". This bit the first version of this script.
cd "${BUILD_DIR}"
mkdir -p bin
case "$(uname -m)" in
    x86_64)
        set -- make -e -C src
        ;;
    aarch64|arm64)
        set -- make -e -C src arm_neon=1 aarch64=1 \
            "CPPFLAGS=-g -Wall -O2 -DHAVE_KALLOC -fopenmp -std=c++11 -Wno-sign-compare -Wno-write-strings -Wno-unused-but-set-variable -fno-tree-vectorize -D_FILE_OFFSET_BITS=64 -fsigned-char"
        ;;
    *)
        echo "unsupported arch: $(uname -m)" >&2
        exit 1
        ;;
esac

# Builds src/ and links bin/winnowmap directly -- deliberately not the
# top-level Makefile's `winnowmap` target, which continues into the
# bundled ext/meryl step that fails on trixie (gotcha (b) above).
"$@"
g++ -g -Wall -O2 -DHAVE_KALLOC -fopenmp -std=c++11 \
    src/main.o -o bin/winnowmap -Lsrc -lwinnowmap -lm -lz -lpthread

mkdir -p "${INSTALL_DIR}/bin"
cp bin/winnowmap "${INSTALL_DIR}/bin/winnowmap"
chmod +x "${INSTALL_DIR}/bin/winnowmap"

INSTALLED_VERSION="$("${INSTALL_DIR}/bin/winnowmap" --version)"

cd /
rm -rf "${BUILD_DIR}"

apt-get purge -y git build-essential zlib1g-dev
apt-get autoremove -y
rm -rf /var/lib/apt/lists/*

echo "winnowmap ${INSTALLED_VERSION} installed:"
du -sh "${INSTALL_DIR}"
