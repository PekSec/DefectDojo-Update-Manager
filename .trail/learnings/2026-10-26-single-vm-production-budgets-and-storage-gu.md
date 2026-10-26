---
title: Single VM production budgets and storage recovery checks
author: Güven
date: 2026-10-26
branch: main
touches:
  - dd_manager.py
  - config.example.json
  - config.production.example.json
  - docs/production.md
  - tests/test_manager.py
  - systemd/defectdojo-backup.service
---

## Decided
Keep the pilot defaults and use config.production.example.json as the optional single VM starting profile for 4,000 assets and about 500,000 accumulated findings. Worker concurrency stays fixed at two initially; change it by measurement, never CPU count. Render PostgreSQL 18 command-line settings and the pinned uWSGI environment options without modifying release bundles. Preserve target-host tuning/timeouts/mount settings on restore, including legacy configs with only original keys.

## Tried and ruled out
The pinned nginx entrypoint does not consume generic upload/timeout env variables. Adapt its writable container config idempotently; a repeated container start must not append duplicate directives. A pre-drain disk measurement alone is stale when imports grow the database: measure and check again after draining. Do not require an unavailable backup-copy destination to reopen a healthy local application or restore from an already available copy; require it for new backup/upgrade and audit it in doctor.

## Watch out
Verify DockerRootDir, containerd image-store location, named and live volume mounts. Canonicalize the image-store path so symlinks cannot hide use of the OS disk. Missing remote backup mount must fail before ingress closes, and before/after copying. Restore retains old generations and needs additional new-generation plus WAL/index headroom; raw-size metadata and configured legacy estimates are thresholds, not reservations or capacity guarantees. Backup/restore have independent per-command deadlines; systemd must not kill the whole maintenance on a short fixed deadline. Maintenance timing spans drain, backup/copy, migration and reopening, including failed-window recovery; a command refused the lock cannot rewrite that report. No 500,000-finding load test, real large import/reimport or large-volume lifecycle was performed for this change.
