# Security scan verdict — 20 July 2026

The first part of this verdict is deliberately narrow. It applies only to the
`app` and `app-ml` images built for `linux/amd64` from
`python:3.11.15-slim-trixie@sha256:00af38ae2ed311628970782e8a2d7f014d8909dbc63cb97bc0a158187f4db045`,
with Debian package `perl-base=5.40.1-6`. Separate, equally narrow verdicts for
the pinned PostgreSQL and Qdrant images are recorded below. No VEX verdict
applies to Nginx, Redis, Squid, Certbot, HAProxy, scanner images, another
architecture, or a different package PURL.

## Evidence

- scanner: Trivy `0.64.1`, pinned by digest;
- vulnerability DB `UpdatedAt`: `2026-07-20T07:44:03Z`;
- unsuppressed scan found three Critical findings, all mapped by Trivy to the
  exact `perl-base` PURL above;
- release architecture reported by the container is `amd64`;
- `Archive::Tar` cannot be loaded in the image;
- the application tree and virtual environment contain zero `/usr/bin/perl`
  shebangs;
- both images start Python directly as the unprivileged `app` user.

Raw Trivy JSON and SBOM remain in the ignored private security-scan directory;
they are not committed because package paths can disclose unnecessary runtime
detail.

## Scoped decisions

1. `CVE-2026-8376` is not applicable to this `amd64` build: the upstream issue
   requires a 32-bit Perl build. Debian also records it as a minor/no-DSA issue
   for Trixie.
2. `CVE-2026-42496` is not applicable to the runtime: the affected
   `Archive::Tar` module is absent and the service performs no Perl archive
   extraction. Debian records the Trixie fix as postponed and the issue as
   minor.
3. `CVE-2026-13221` exists in the packaged Perl generation, but has no reachable
   service path: the service does not invoke Perl or compile attacker-controlled
   Perl regular expressions. Any introduction of a Perl entrypoint invalidates
   this verdict.

Authoritative references:

- <https://security-tracker.debian.org/tracker/CVE-2026-8376>
- <https://security-tracker.debian.org/tracker/CVE-2026-42496>
- <https://security-tracker.debian.org/tracker/CVE-2026-13221>

## Fail-closed boundary

`security/trivy-app-vex.yaml` scopes every application exception to the exact
package PURL and expires it on **2026-07-27**. Trivy receives this file only for
`app` and `app-ml`. An expired entry, changed architecture, Debian release,
`perl-base` version, or changed PURL becomes an unsuppressed blocker
automatically.

## Infrastructure image refresh and narrow verdicts

The original pinned images were rejected by the 20 July database. They were
replaced only after scanning the current official candidates:

- PostgreSQL remains `16.14-alpine3.24`, rebuilt at digest
  `sha256:57c72fd2a128e416c7fcc499958864df5301e940bca0a56f58fddf30ffc07777`;
- Qdrant is upgraded from `1.10.1` to `1.18.3`, digest
  `sha256:0bd98fa7977f1e75694779359ca4e212822e5a71334e28421182f72f209d5286`;
- Nginx is upgraded from `1.27.5` to `1.30.4`, digest
  `sha256:97d490c12ba55b4946b01546d1c3ed324e8d41ab1c9fcb2a616aa470620e5b46`;
- Certbot is upgraded from `5.4.0` on EOL Alpine to `5.7.0`, digest
  `sha256:34ee91d2f43008eb78a007d22f23ed4b2eaa9a454cb27ca2c042b49527a695b4`;
- the secretless L4 edge relay uses HAProxy `3.4.2-alpine`, digest
  `sha256:0878b11eb64c433be1b0f578a584b8aca12f6caaa64c8f239b8b556c0dd5eeeb`.

Nginx, Certbot, Redis, Squid and HAProxy have zero active Critical findings and
no VEX.
The refreshed Qdrant image removed seven actionable Critical findings; its only
remaining findings are the same three `perl-base` records. The image is amd64,
has no `Archive::Tar` and no Perl entrypoints, and starts the Rust binary
directly. `security/trivy-qdrant-vex.yaml` records that exact-PURL verdict until
27 July. A real isolated smoke proved qdrant-client `1.11.3` can create, upsert
and search against Qdrant `1.18.3`.

The refreshed PostgreSQL image has one scanner finding in the Go standard
library embedded in `gosu`: `CVE-2025-68121`. The authoritative Go advisory
limits the affected symbols to `crypto/tls` session resumption, while gosu's
reviewed source is a local uid/gid switch-and-exec program with no network/TLS
path. `security/trivy-postgres-vex.yaml` scopes this exact PURL until 27 July.
Reference: <https://pkg.go.dev/vuln/GO-2026-4337>.

The Qdrant/PostgreSQL VEX files are passed only to their corresponding image
scan. They are never used for other infrastructure images.

This verdict does not make the overall recovery release `GO`. Provider-side
Gate 0, clean-host acceptance, new credentials, runtime checks, correction
cycle, and HDE smoke remain mandatory.
