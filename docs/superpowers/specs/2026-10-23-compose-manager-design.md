# DefectDojo Open Source maintenance design

Implement the user's requested repository-local deployment, without deploying to
the host or changing upstream. Use Python 3 standard library and Docker Compose
v2.24+; no Pro tooling, Kubernetes, Watchtower or SaaS dependency.

Vendor only each release's Compose file and license, plus source commit, hashes,
and immutable image references. Generate a deployment from that definition:
remove builds and source mounts, require generated secrets, bind HTTP only to
loopback, persist PostgreSQL, media and Valkey. Environment configuration lives
outside the release bundle. HTTPS terminates at an operator-managed local reverse
proxy; only loopback HTTP is published.

The single CLI handles init, install, start, stop, status, doctor, backup,
check-update, upgrade and restore. File locking serializes commands; subprocesses
have deadlines and secret-bearing output is never printed. Dry-run returns before
any write or subprocess invocation. systemd units are delivered but never installed.

Quiesce nginx and uwsgi gracefully, stop beat, require successful Celery inspection
and zero active/reserved/scheduled/queued/unacknowledged work, then stop workers.
Timeout leaves maintenance state and never force-kills imports. Backups contain a
custom pg_dump, media tar, config/secrets, distribution files, image identities,
checksums. Validate before publication, copy to optional mounted destination before
retention. Upgrade backups are retained separately from daily retention.

Upgrade requires an explicit target and a reviewed, integration-tested edge in a
local policy. Pull before downtime; backup before changing the active release;
wait for initializer exit 0 and verify PostgreSQL, Valkey, Django migrations,
Celery ping, scheduler, and HTTP before reopening nginx. Failure leaves writers
stopped and a recovery marker. Restore validates all checksums, archive paths,
bundle and database archive before destructive operations and requires typing the
project-specific confirmation. Never automatically roll back schema migrations.

Alternatives: shell would need extra parsing tools and make failure-state tests
harder; copying the whole upstream tree is unnecessary. Python plus vendored
deployment files is the smallest maintainable implementation. Future upgrades are
blocked until a real rehearsal records validation; no guessed compatible path.

Validation: stdlib unittest with injected Docker boundary failures, real Compose
config validation, Python static checks, and an opt-in isolated Docker lifecycle
test. Document actual results and unverified upgrade paths. Preserve preexisting
untracked AGENTS.md, .trail and .claude files.
