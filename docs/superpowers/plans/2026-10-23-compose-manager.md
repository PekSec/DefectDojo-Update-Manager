# DefectDojo Compose Manager Implementation Plan

> Execute inline with superpowers:executing-plans. User requested implementation
> through delivery in this repository; no deployment, host installation or push.

**Goal:** Reproducible Open Source deployment with conservative backup and recovery.
**Architecture:** One stdlib Python CLI, immutable upstream release bundles,
separate local JSON configuration, opt-in systemd timers.
**Tech Stack:** Python 3.10+, Docker Engine, Docker Compose 2.24+.
**Spec:** docs/superpowers/specs/2026-10-23-compose-manager-design.md

## Global constraints

- Pin release 3.4.202 and every image digest; use actual upstream service names.
- No host installation, remote push, Pro dependencies or automatic upgrade.
- No destructive restore without project-specific confirmation; dry-run is inert.
- English code/logs, Turkish operator documentation.

## Review focus

- Interrupted imports: no SIGKILL, purge or silent job loss.
- Recovery marker survives migration/health failures and blocks ordinary start.
- Missing/corrupt backup data fails before destructive restore.
- Image pull/backup failures cannot initiate migration.
- Timers and retention cannot remove upgrade recovery backups.

## Tasks

- [x] 1. Fetch immutable upstream Compose/license and image digests; record sources.
  Create config.example.json, release policy, and tests for configuration/bundles.
- [x] 2. Write failing lifecycle/failure-injection tests, run them, implement CLI
  with locking, dry-run, secret generation, bounded Docker calls and health checks.
- [x] 3. Write failing backup/restore and upgrade-gate tests, implement checksums,
  media/database snapshots, retention/copy, maintenance markers and confirmation.
- [x] 4. Add opt-in isolated Docker lifecycle test, systemd units, README/runbooks.
  Run unittest, compile/static checks, Compose validation, Docker rehearsal when
  feasible. Request independent review, fix material issues, run trail check.

## Progress / rulings

- Native execution; preserve user's current workspace and do not commit unless asked.
- Default release is 3.4.202; 3.4.201 is the rehearsal baseline. Only a source/target
  pair with a completed real rehearsal may be enabled; adding a new edge is documented.
- Bundled 3.4.201 as the isolated rehearsal baseline. Both upstream commits,
  licenses and all image digests verified through official GitHub/Docker Hub.
- Independent review found shared normalized network names, initializer retry
  interruption, and a late restore marker. All three have RED-to-GREEN regressions.
- Preserve long HTTP imports using uWSGI's private stats socket before stopping;
  no FIFO mercy timer or signal is used to interrupt active requests.
- `make test static validate` passes. Two real isolated lifecycle rehearsals passed
  (927.249s and 481.688s); only 3.4.201 -> 3.4.202 is enabled, bound to both commits.
- Timeout cleanup kills the local client process group, never daemon containers;
  a real descendant-survival regression proves the boundary. Installed project
  identity is persisted so an accidental config rename cannot silently select
  new empty volumes. Fresh default 3.4.202 smoke verification passed, including
  all service probes and safe drain. Real read-only GitHub release check also passed.
- Final PostgreSQL 18 parent-volume layout passed a complete real lifecycle in
  427.226s. No test containers remain; the original 19 user volumes are preserved.
  All 34 unit tests, Compose rendering, Python syntax checks and adapted systemd
  verification passed. Trail's documentation false positives are explained in the
  validation record and a new learning. No host installation, commits or pushes.
