---
title: PostgreSQL 18 named volume layout
author: Güven
date: 2026-10-23
branch: main
touches:
  - dd_manager.py
  - tests/test_manager.py
  - docs/upgrades.md
---

## Decided
The pinned postgres:18.6-alpine image declares VOLUME /var/lib/postgresql. The runtime adapter mounts the named database volume at that parent and sets PGDATA=/var/lib/postgresql/18/docker, matching the official PostgreSQL 18 image layout. Release source files stay unmodified. Backups and restores remain logical dumps.

## Tried and ruled out
The upstream release Compose mounts the named data volume at the older /var/lib/postgresql/data subdirectory. Real lifecycle tests showed a separate empty anonymous parent volume surviving a restore's container removal. Image metadata confirmed the parent declaration; the Django image declares no volumes.

## Watch out
Raw data directories from the old layout are not interchangeable with this layout. Initializing a new deployment and restoring a logical dump is the supported transition. Verify both restored data and cleanup of test volumes; preserve old production volume generations until the operator has verified recovery.
