---
title: Pinned DefectDojo maintenance and recovery boundaries
author: Güven
date: 2026-10-23
branch: main
touches:
  - dd_manager.py
  - README.md
  - releases
  - tests/test_manager.py
  - tests/test_integration.py
---

## Decided
Use official commit-bound Compose/license bundles and immutable Docker Hub image digests. Keep runtime configuration and generated secrets separate from release files. Only explicit source/target pairs backed by an isolated rehearsal may enter upgrade-paths.json. Database or broker image changes need a dedicated data migration.

## Tried and ruled out
Do not deploy upstream's example Compose directly: it contains public ports, example secrets, builds and moving image defaults. Do not roll schema changes back by restarting an old image or automatically restoring a backup. Normalized Compose network names must be removed so separate projects cannot share service DNS.

## Watch out
An initializer timeout does not mean migration stopped: await the existing matching container on retry and refuse restore/backup/stop while it runs. Record restore state before stopping any writer. Wait for uWSGI active requests, including disconnected clients' synchronous imports, and drain Celery active/reserved/scheduled plus all broker lists/unacknowledged deliveries. Restore to a new volume generation, preserve previous metadata, and require project-specific confirmation. Never label a mocked or unfinished lifecycle test as real upgrade evidence.
