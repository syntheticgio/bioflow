#!/bin/sh
# Install Polypolish from its pinned upstream release binary.
#
# Not in Debian -- checked against the running image on 2026-08-05 *with* an
# `apt-get update` first, since a bare `apt-cache policy` reports
# "Candidate: (none)" for every package regardless of what the repository
# carries, and that stale-cache false negative already produced one wrong
# claim in this repo (see the iVar design doc). No candidate for `polypolish`,
# `pypolca`, or `polca`.
#
# The release binary rather than a cargo build: upstream ships a musl-static
# x86_64 binary with no runtime dependencies, so there is nothing to compile
# and no toolchain to add to this image for one tool.
#
# arm64 is deliberately unsupported. The v0.7.1 release assets are
# linux-x86_64-musl, macos-aarch64 and macos-x86_64 -- there is no
# linux-aarch64 build, and building one from source would produce a tool with
# no aligner to pair with, since bwa-mem2 (which Polypolish requires, because
# it needs all-alignment output) has its own arm64 problem this image already
# works around. A tool that installs and then cannot run is worse than an
# honest "not available on this architecture", which is what tools.polypolish()
# reports instead. See docs/superpowers/specs/2026-08-05-polypolish-design.md.

set -eu

POLYPOLISH_VERSION="${POLYPOLISH_VERSION:-0.7.1}"
TARGETARCH="${TARGETARCH:-amd64}"

if [ "$TARGETARCH" = "arm64" ]; then
    echo "Skipping Polypolish: no linux-aarch64 release build upstream."
    exit 0
fi

URL="https://github.com/rrwick/Polypolish/releases/download/v${POLYPOLISH_VERSION}/polypolish-linux-x86_64-musl-v${POLYPOLISH_VERSION}.tar.gz"

echo "Installing Polypolish ${POLYPOLISH_VERSION}..."
curl -fsSL "$URL" -o /tmp/polypolish.tar.gz
tar -xzf /tmp/polypolish.tar.gz -C /tmp
install -m 0755 /tmp/polypolish /usr/local/bin/polypolish
rm -f /tmp/polypolish.tar.gz /tmp/polypolish

polypolish --version
