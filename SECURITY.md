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

## Intended deployment model

BioFlow is designed for a **single trusted user on their own machine**.
`docker compose up` (the only supported deployment method) binds to
`127.0.0.1` and is not a hosted or multi-tenant service. The application
should never be exposed to untrusted networks without additional
authentication.

## Profile selection is not authentication

The `X-BioFlow-Profile` header used to separate workspaces is an
organizational convenience, **not access control**. Any process that can
reach the API can read and modify any profile's data. The implementation
is in `app/api/deps.py`.

Before exposing the API to any non-loopback interface, place it behind
something that does authenticate — a reverse proxy with auth, an SSH tunnel,
or a VPN. The application itself will not stop anyone.

## Known security boundaries

### SSH node provisioning

Node provisioning connects over SSH to remote machines to install and manage
pipeline workers. This is the only part of the application that communicates
over a network by design:

- A private key is transmitted to the remote host and installed in
  `authorized_keys`.
- Commands are executed on the remote machine using that key.
- Host key verification uses trust-on-first-use (TOFU) — see
  `app/services/node_ssh.py` for the implementation.

### AI provider API keys

AI provider API keys are stored encrypted at rest using Fernet symmetric
encryption (`app/services/ai/crypto.py`). However, the decryption key lives
on the same machine and is accessible to anything that can reach the API
(per the profile note above).

### Pipeline parameter sanitization

User-supplied pipeline parameters are validated against an allowlist and
reject path separators (`/`, `\`, `~`) before reaching tool command lines.
This is a deliberate security boundary between user input and the subprocess
that runs each pipeline tool. See `app/services/params_sanitizer.py` for
the implementation.

### Container escape surface

Pipeline jobs run as sibling containers on the Docker socket. Only run
software you trust.

If you find something that looks like a vulnerability, please report it —
even if it only works against a threat model outside the intended one. The
worst outcome is assuming someone else will notice.
