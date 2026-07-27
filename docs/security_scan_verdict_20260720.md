# Security scan verdict — 20 July 2026; fail-closed re-review 27 July 2026

The first part of this verdict is deliberately narrow. It applies only to the
`app` and `app-ml` images built for `linux/amd64` from
`python:3.11.15-slim-trixie@sha256:00af38ae2ed311628970782e8a2d7f014d8909dbc63cb97bc0a158187f4db045`,
with Debian package `perl-base=5.40.1-6`. Separate, equally narrow verdicts for
the pinned PostgreSQL and Qdrant images are recorded below. No scoped exception
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

The clean recovery-host scan on 22 July refreshed the pinned Trivy DB and
correctly stopped before provider credentials on the newly published
`CVE-2026-57433`. The finding was mapped to the same exact `perl-base=5.40.1-6`
PURL. The affected implementation is `Storable` before 3.41, specifically its
crafted `SX_HOOK` deserialization path. Debian's authoritative file list for
the exact Trixie `perl-base` package contains no `Storable.pm` or Storable
shared object; the runtime Dockerfile adds no Debian packages and the module is
not loadable in the built application images. The exception below therefore
records an absent affected component, not a generic acceptance of a reachable
Critical vulnerability. The server scan must still be repeated from a fresh
SHA-bound evidence directory after this policy change.

The next fresh scan correctly stopped again on `CVE-2026-59873`, mapped to
`pkg:npm/tar@7.5.16` in the exact pinned Qdrant digest. The upstream advisory
requires a process to invoke node-tar's parse/extract path on an untrusted
archive; affected releases lack cumulative decompression and entry-count
limits. Exact-image inspection found no `node`, `npm`, or `npx` executable and
zero `node_modules/tar` files. The only `tar@7.5.16` record is inventory
metadata in `/qdrant/static/qdrant-web-ui.spdx.json`; the final image starts a
shell entrypoint which launches the Rust Qdrant binary. The package is therefore
not executable or reachable in this runtime. This is a temporary metadata-only
bridge, not acceptance of a reachable archive-extraction service, and still
requires a new SHA and fresh complete scan.

Raw Trivy JSON and SBOM remain in the ignored private security-scan directory;
they are not committed because package paths can disclose unnecessary runtime
detail.

The fail-closed re-review on 27 July confirmed that the exact pinned image
digests, Debian package PURL, architecture and runtime entrypoints covered by
this verdict are unchanged. Fresh offline exact-image inspection again found
`Archive::Tar` and `Storable` absent, zero Perl shebangs in the application and
virtual environment, no Node/npm/npx executable or runtime `node_modules/tar`
path in Qdrant, and only the known Web UI SPDX inventory record. The pinned
PostgreSQL image still contains `gosu 1.19` built with Go 1.24.6; upstream
`gosu` 1.19 source remains a local uid/gid switch-and-exec utility without a
TLS session-resumption path. Debian, node-tar and Go authoritative advisories
still describe the same affected conditions. The exact PURL-bound policies
are therefore renewed through 10 August 2026; any image, package, architecture
or entrypoint change invalidates this renewal.

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
4. `CVE-2026-57433` is not present in these images: exploitation requires a
   crafted blob to reach `Storable::thaw` or `Storable::retrieve`, while the
   exact `perl-base` package does not ship Storable and the Python service has no
   Perl deserialization path. A base-image/package-content/entrypoint change
   invalidates this verdict.

Authoritative references:

- <https://security-tracker.debian.org/tracker/CVE-2026-8376>
- <https://security-tracker.debian.org/tracker/CVE-2026-42496>
- <https://security-tracker.debian.org/tracker/CVE-2026-13221>
- <https://security-tracker.debian.org/tracker/CVE-2026-57433>
- <https://packages.debian.org/trixie/amd64/perl-base/filelist>
- <https://github.com/Perl/perl5/commit/e4f681784bcdeaa91ff02a2fa4cdcae5c46779d7>
- <https://github.com/isaacs/node-tar/security/advisories/GHSA-23hp-3jrh-7fpw>
- <https://github.com/isaacs/node-tar/commit/2812e9338665659b183aa7226518c307044957d3>
- <https://github.com/qdrant/qdrant/blob/v1.18.3/Dockerfile>
- <https://github.com/qdrant/qdrant/blob/v1.18.3/tools/sync-web-ui.sh>

## Fail-closed boundary

`security/trivy-app-ignore.yaml` scopes every application exception to the exact
package PURL and expires it on **2026-08-10**. Trivy receives this file only for
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
without an exception policy.
The refreshed Qdrant image removed seven actionable Critical findings; its
20 July DB findings were the same three `perl-base` records. Exact-image
inspection confirms that Storable and Archive::Tar are not loadable, no Perl
entrypoint exists, and the service starts its Rust binary directly. The later
`CVE-2026-59873` record is confined to the embedded Web UI SPDX inventory:
Node/npm/npx and executable node-tar files are absent. The Qdrant policy scopes
all five records to their exact PURLs until 10 August; a digest, inventory,
runtime-tool or entrypoint change invalidates the corresponding decision. The
zero-active verdict remains pending until a new fresh full scan records these
findings as suppressed. A real isolated smoke proved qdrant-client `1.11.3`
can create, upsert and search against Qdrant `1.18.3`.

The refreshed PostgreSQL image has one scanner finding in the Go standard
library embedded in `gosu`: `CVE-2025-68121`. The authoritative Go advisory
limits the affected symbols to `crypto/tls` session resumption, while gosu's
reviewed source is a local uid/gid switch-and-exec program with no network/TLS
path. `security/trivy-postgres-ignore.yaml` scopes this exact PURL until 10 August.
Reference: <https://pkg.go.dev/vuln/GO-2026-4337>.

The Qdrant/PostgreSQL scoped ignore policies are passed only to their corresponding image
scan. They are never used for other infrastructure images.

This verdict does not make the overall recovery release `GO`. Provider-side
Gate 0, clean-host acceptance, new credentials, runtime checks, correction
cycle, and HDE smoke remain mandatory.
