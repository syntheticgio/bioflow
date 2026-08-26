// The launcher's own version, injected at build time from launcher/package.json
// by vite.config.ts. It is deliberately *not* read from tauri.conf.json: on a
// pre-release that file carries the core version only (`0.6.0` for
// `0.6.0-alpha`) because the macOS CFBundleShortVersionString derived from it
// must be numeric, so it cannot tell an alpha build from the release. See
// VERSION.md, "The file Tauri actually reads".
//
// ops/release.sh bumps package.json on every cut, so this tracks the shipped
// version with nothing to remember to update by hand -- which is the bug this
// replaced (#808): seven copies of a literal "0.1.0" left over from the first
// release, four versions out of date.

declare const __LAUNCHER_VERSION__: string;

/** The version string as released, pre-release suffix included: "0.6.0-alpha". */
export const LAUNCHER_VERSION: string = __LAUNCHER_VERSION__;

/** The version as shown in a status line: "Launcher 0.6.0-alpha". */
export const LAUNCHER_VERSION_LABEL = `Launcher ${LAUNCHER_VERSION}`;
