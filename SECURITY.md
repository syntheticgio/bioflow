# Security Policy

## Supported Versions

BioFlow is a local, single-user tool. Only the latest release on the default
branch (`main`) receives security fixes. Because the project is
non-commercial and single-maintainer, there is no LTS line or backporting
policy — upgrade to the latest tagged release.

| Version | Supported |
| --- | --- |
| latest (`main`) | ✅ |
| older releases | ❌ — upgrade before reporting |

## Reporting a Vulnerability

**Do not open a public issue for security vulnerabilities.**

Email the maintainer directly at **[bioflow-security@syntheticgio.com]**
or, if you prefer not to use email, [open a private advisory on
GitHub](https://github.com/syntheticgio/bioflow/security/advisories/new).

Please include:

- A description of the vulnerability and the impact.
- Steps to reproduce or a proof-of-concept.
- Your preferred attribution (or whether you'd like to remain anonymous).

You should receive a reply within 7 days. If the report is confirmed, a fix
will be prepared and a release cut within a reasonable timeframe. A CVE, if
warranted, will be requested with your chosen attribution name.

## Security Considerations

BioFlow is designed for a **single trusted user on their own machine**. By
default, the API binds to `127.0.0.1` and requires no authentication. See
the [Security section of the README](README.md#security) for the full threat
model before exposing the application to any network.

### Known limitations

- **No authentication by design.** The `X-BioFlow-Profile` header that
  separates workspaces is an organizational convenience, not access control.
- **Container escape surface.** Pipeline jobs run as sibling containers on
  the Docker socket. Only run software you trust.
- **AI provider keys.** API keys are stored encrypted at rest (Fernet), but
  the decryption key lives on the same machine.

If you find something that looks like a vulnerability, please report it —
even if it only works against a threat model outside the intended one. The
worst outcome is assuming someone else will notice.
