# RepoLaunch local integration services

Run start-integrations.bat from the project root in your ordinary Windows session.
The script prepares and validates real services, then starts a fresh RepoLaunch
backend with the verified settings. It does not remove unrelated containers or
delete data on failure.

All published ports bind 127.0.0.1. MySQL, Milvus, MinIO, PostgreSQL, Redis and
etcd persist under ../.repolaunch/services in this F-drive project. ClickHouse
alone uses the project-scoped named volumes clickhouse-data and clickhouse-logs,
inside Docker Desktop's Linux disk image. This installation's disk image has
already been moved to F drive. Images and container layers share that configured
location; this script does not change Docker Desktop settings.

The full development stack can consume 8-12GB RAM or more and several GB disk.
The per-container limits are development limits, not production sizing advice.
This bundle is for local development, not an internet-exposed production server.
Milvus needs the upstream-recommended seccomp:unconfined setting, no privileged
mode and no Docker socket mount. Do not expose its port beyond localhost.

Credentials are generated once in ignored local services.env; missing credentials
beside existing data cause a stop rather than an unsafe password reset.
Langfuse login details are written to local credentials.txt after verification.
Never share these files. No cloud Langfuse account is needed.

SQLite task history is copied transactionally only if the MySQL target task table
is empty. The source SQLite file is opened read-only and left untouched. Existing
nonempty MySQL history is never overwritten.

Published default ports:
- MySQL 13306
- Milvus 19530; health 19091
- MinIO 19000; console 19001
- Langfuse 30300; worker 30301

Official references consulted:
- https://raw.githubusercontent.com/docker-library/official-images/master/library/mysql
- https://raw.githubusercontent.com/docker-library/official-images/master/library/postgres
- https://raw.githubusercontent.com/docker-library/official-images/master/library/redis
- https://raw.githubusercontent.com/milvus-io/milvus/v2.6.12/deployments/docker/standalone/docker-compose.yml
- https://github.com/langfuse/langfuse/releases/tag/v3.202.0
- https://raw.githubusercontent.com/langfuse/langfuse/v3.100.0/docker-compose.yml
- https://langfuse.com/self-hosting/administration/headless-initialization
- https://langfuse.com/self-hosting/deployment/infrastructure/clickhouse
- https://langfuse.com/self-hosting/infrastructure/blobstorage
- https://github.com/minio/minio/releases/tag/RELEASE.2025-04-22T22-12-26Z
- https://github.com/ClickHouse/ClickHouse/releases/tag/v25.3.9.72-lts

The Milvus 2.6.12 upstream compose explicitly selects the 2.6.11 server image.
The shared MinIO uses separate Langfuse and Milvus buckets, as supported by the
Milvus configuration documentation. Langfuse headless initialization creates
the local organization, project, API keys and admin user.

The selected v3 Langfuse release preserves compatibility with the existing
project SDK. Image tags are exact versions; after deployment verify them with
Docker's image digests when stronger supply-chain pinning is required.

MinIO packaging note: upstream MinIO Docker Hub tags were unavailable at the
live registry check. This local bundle pins the vendor's public legacy archive
bitnamilegacy/minio:2025.7.23-debian-12-r5, verified through the live Docker Hub
tag API. The archive receives no further updates. Keep its published ports on
localhost and use a maintained object store for production. The executable is
called by its absolute vendor path; the health probe uses Bash builtins rather
than relying on curl being installed.
Reference: https://hub.docker.com/r/bitnamilegacy/minio/tags

For a failed startup, run diagnose-integrations.bat. It only reads this Compose
project's container state and recent logs, redacts local generated credentials,
and writes .repolaunch/services/diagnostics.json. It never starts, stops,
restarts, or removes a service. Deployment failures also attempt this report
automatically. The report omits container environment variables.

The web service explicitly binds HOSTNAME=0.0.0.0 and PORT=3000 so the in-container
localhost health probe and published port use the intended interface. This aligns
with upstream's current Compose build configuration and web Helm template:
https://github.com/langfuse/langfuse/blob/main/docker-compose.build.yml
https://github.com/langfuse/langfuse-k8s/blob/main/charts/langfuse/templates/web/deployment.yaml

Windows storage correction: the first real startup report showed ClickHouse
migration failing with filesystem rename Permission denied under its NTFS bind
mount, while PostgreSQL migrations had completed and web OOMKilled was false.
ClickHouse now uses Linux named volumes to provide the required filesystem
semantics, following the upstream Langfuse Compose storage pattern. The old
.repolaunch/services/clickhouse-data and clickhouse-logs bind directories remain
untouched for inspection; the launcher never moves or deletes them. All other
service mounts are unchanged. A retry uses compose up --detach --wait, never down
or volume deletion. Evidence stays in the ignored local diagnostics.json report.
Reference: https://github.com/langfuse/langfuse/blob/v3.100.0/docker-compose.yml

The same explicit HOSTNAME=0.0.0.0 is applied to the worker: the first diagnostic
showed its HTTP server listening on the injected container hostname while the
localhost health probe failed. This corrects that confirmed interface mismatch.
Upstream report and maintainers' fix:
https://github.com/langfuse/langfuse-k8s/issues/327
https://github.com/langfuse/langfuse/issues/6774
