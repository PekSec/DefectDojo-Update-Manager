"""Conservative maintenance for pinned DefectDojo Open Source Compose releases."""

import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tarfile
import time
import urllib.request
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parent
SERVICES = {'nginx', 'uwsgi', 'initializer', 'celeryworker', 'celerybeat', 'postgres', 'valkey'}
WRITERS = ['nginx', 'uwsgi', 'celerybeat', 'celeryworker']
DEFAULT_VERSION = '3.4.202'
GIB = 1024**3
# Only newly introduced keys are optional; missing original keys still indicate damage.
OPTIONAL_DEFAULTS = {
    'profile': 'pilot', 'worker_concurrency': 2, 'uwsgi_processes': 4, 'uwsgi_threads': 4,
    'postgres_settings': {}, 'resource_limits_gib': {},
    'upload_max_mb': 800, 'request_timeout': 1800, 'upload_timeout': 60,
    'data_upload_max_memory_mb': 8, 'backup_timeout': 7200, 'restore_timeout': 14400,
    'data_mount': None, 'image_storage_dir': None, 'backup_copy_mount': None,
    'disk_reserve_gib': 5, 'backup_estimate_gib': 10, 'restore_estimate_gib': 20,
}
PG_MEMORY = {'shared_buffers', 'effective_cache_size', 'work_mem', 'maintenance_work_mem',
             'autovacuum_work_mem', 'max_wal_size', 'min_wal_size'}
PG_COUNTS = {'max_connections', 'max_parallel_workers_per_gather', 'max_parallel_workers',
             'max_parallel_maintenance_workers', 'max_worker_processes', 'autovacuum_max_workers'}


class Failure(Exception):
    """An operator-actionable failure, with no secret-bearing command output."""


def log(message):
    print(f'{dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")} {message}', flush=True)


def read_json(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise Failure(f'Cannot read valid JSON: {path}') from exc


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_name(path.name + '.tmp')
    with open(temp, 'w', opener=lambda p, flags: os.open(p, flags, 0o600)) as stream:
        json.dump(data, stream, indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    temp.replace(path)


def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def safe_relative(name):
    path = PurePosixPath(name)
    return not path.is_absolute() and '..' not in path.parts and '\\' not in name


def validate_media(path):
    try:
        with tarfile.open(path, 'r:gz') as archive:
            for member in archive:
                if not safe_relative(member.name) or not (member.isfile() or member.isdir()):
                    raise Failure('Unsafe media archive path or non-regular entry')
                if member.isfile():
                    with archive.extractfile(member) as stream:
                        while stream.read(1024 * 1024):
                            pass
    except (tarfile.TarError, EOFError, OSError) as exc:
        raise Failure('Invalid media archive') from exc


def bundle(directory):
    manifest = read_json(directory / 'release.json')
    if not re.fullmatch(r'\d+\.\d+\.\d+', manifest['version']) or not re.fullmatch(r'[a-f0-9]{40}', manifest['commit']):
        raise Failure('Release bundle must name an exact stable version and commit')
    if set(manifest['files']) != {'compose.json', 'docker-compose.yml', 'LICENSE.md'}:
        raise Failure('Incomplete release bundle')
    for name, digest in manifest['files'].items():
        if sha256(directory / name) != digest:
            raise Failure(f'Release file checksum mismatch: {name}')
    doc = read_json(directory / 'compose.json')
    if set(doc['services']) != SERVICES:
        raise Failure('Unsupported service topology; review the new release architecture')
    for name, service in doc['services'].items():
        if service['image'] != manifest['images'][name] or not re.search(r'@sha256:[0-9a-f]{64}$', service['image']):
            raise Failure(f'Unpinned or inconsistent image: {name}')
        if service.get('build') or any(v['type'] == 'bind' for v in service.get('volumes', [])):
            raise Failure('Release bundle must not build or mount upstream source')
    return manifest, doc


def verify_backup(directory):
    manifest = read_json(directory / 'manifest.json')
    required = {'database.dump', 'media.tar.gz', 'config.json', 'secrets.json',
                'compose.json', 'volumes.json', 'bundle/release.json',
                'bundle/compose.json', 'bundle/docker-compose.yml', 'bundle/LICENSE.md'}
    if manifest.get('format') != 1 or set(manifest.get('files', {})) != required:
        raise Failure('Unsupported or incomplete backup manifest')
    for name, digest in manifest['files'].items():
        path = directory / name
        if not safe_relative(name) or path.is_symlink() or not path.is_file():
            raise Failure('Unsafe or missing backup member')
        if sha256(path) != digest:
            raise Failure(f'Backup checksum mismatch: {name}')
    if (directory / 'bundle').is_symlink():
        raise Failure('Symlinked backup bundle is not allowed')
    release, _ = bundle(directory / 'bundle')
    if release['version'] != manifest['version'] or manifest['images'] != release['images']:
        raise Failure('Backup version/image mismatch')
    with (directory / 'database.dump').open('rb') as stream:
        if stream.read(5) != b'PGDMP':
            raise Failure('Backup is not a PostgreSQL custom-format dump')
    validate_media(directory / 'media.tar.gz')
    return manifest


# Inspect all broker lists, including Redis priority queues, and in-flight deliveries.
BROKER_CHECK = """
import json
import redis
from django.conf import settings
r = redis.Redis.from_url(settings.CELERY_BROKER_URL)
n = 0
for key in r.scan_iter():
    if r.type(key) == b'list':
        n += r.llen(key)
n += r.hlen('unacked') + r.zcard('unacked_index')
print('DD_RESULT=' + json.dumps(n))
"""
WORK_CHECK = """
import json
from celery import current_app
i = current_app.control.inspect(timeout=5)
counts = []
for method in (i.active, i.reserved, i.scheduled):
    replies = method()
    if not replies:
        raise RuntimeError('No worker inspection reply; worker may be busy')
    counts.append(sum(len(tasks) for tasks in replies.values()))
print('DD_RESULT=' + json.dumps(sum(counts)))
"""


class Manager:
    def __init__(self, root, state):
        self.root = Path(root).resolve()
        self.state = Path(state).resolve()
        self.config = None
        self.secrets = {}

    @contextlib.contextmanager
    def lock(self):
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.state.stat().st_mode & 0o077:
            raise Failure('State directory must have mode 0700')
        with (self.state / 'operation.lock').open('a') as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise Failure('Another maintenance command holds the lock') from exc
            yield

    def init(self):
        if (self.state / 'config.json').exists() or (self.state / 'secrets.json').exists():
            raise Failure('State already initialized; edit config.json, do not regenerate keys')
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        config = read_json(self.root / 'config.example.json')
        credentials = {
            'DD_DATABASE_PASSWORD': secrets.token_hex(24),
            'DD_SECRET_KEY': secrets.token_hex(32),
            'DD_CREDENTIAL_AES_256_KEY': secrets.token_urlsafe(24),
            'DD_ADMIN_PASSWORD': secrets.token_urlsafe(24),
            'DD_JIRA_WEBHOOK_SECRET': secrets.token_hex(24),
        }
        write_json(self.state / 'config.json', config)
        write_json(self.state / 'secrets.json', credentials)
        write_json(self.state / 'volumes.json', {'generation': 'initial'})
        self.config = config
        self.set_phase('new')
        log(f'Initialized configuration and private credentials in {self.state}; review config.json before install')

    def load(self):
        config = read_json(self.state / 'config.json')
        credentials = read_json(self.state / 'secrets.json')
        self.validate_config(config, credentials)
        self.config = config
        self.secrets = credentials
        for name in ['secrets.json', 'config.json']:
            if (self.state / name).stat().st_mode & 0o077:
                raise Failure(f'Private configuration must have mode 0600: {name}')
        if self.phase()['phase'] in ('ready', 'stopped') and self.phase()['version'] != config['version']:
            raise Failure('Do not edit an installed version in config.json; use an explicit reviewed upgrade')
        if self.phase()['phase'] != 'new' and self.phase().get('project') != config['project']:
            raise Failure('Do not rename an installed project; restore into a separate initialized state instead')
        bundle(self.release_dir())

    def validate_config(self, config, credentials):
        defaults = read_json(self.root / 'config.example.json')
        if set(config) - (set(defaults) | set(OPTIONAL_DEFAULTS)) or (set(defaults) - set(OPTIONAL_DEFAULTS)) - set(config):
            raise Failure('Configuration keys must match config.example.json')
        for key, value in OPTIONAL_DEFAULTS.items():
            config.setdefault(key, value.copy() if isinstance(value, dict) else value)
        if config['profile'] not in ('pilot', 'single-vm-production'):
            raise Failure('Invalid profile')
        bounds = {'worker_concurrency': 32, 'uwsgi_processes': 16, 'uwsgi_threads': 16,
                  'upload_max_mb': 4096, 'data_upload_max_memory_mb': 1024,
                  'request_timeout': 86400, 'upload_timeout': 86400,
                  'backup_timeout': 604800, 'restore_timeout': 604800,
                  'disk_reserve_gib': 1048576, 'backup_estimate_gib': 1048576,
                  'restore_estimate_gib': 1048576}
        for key, maximum in bounds.items():
            if type(config[key]) is not int or not 1 <= config[key] <= maximum:
                raise Failure(f'{key} must be an integer from 1 to {maximum}')
        pg = config['postgres_settings']
        if not isinstance(pg, dict) or set(pg) - (PG_MEMORY | PG_COUNTS):
            raise Failure('Unsupported PostgreSQL setting')
        for key, value in pg.items():
            if key in PG_MEMORY:
                valid = isinstance(value, str) and re.fullmatch(r'[1-9][0-9]*(MB|GB)', value)
            else:
                valid = type(value) is int and 0 <= value <= 1000 and (value > 0 or key.startswith('max_parallel'))
            if not valid:
                raise Failure(f'Invalid PostgreSQL setting: {key}')
        limits = config['resource_limits_gib']
        if not isinstance(limits, dict) or set(limits) - SERVICES or any(type(v) is not int or not 1 <= v <= 64 for v in limits.values()):
            raise Failure('resource_limits_gib must map service names to integer GiB limits (1..64)')
        if config['profile'] == 'single-vm-production' and (set(limits) != SERVICES or sum(limits.values()) > 52):
            raise Failure('Production memory budget requires all service limits totaling at most 52 GiB; retain host/cache headroom')
        for key in ('data_mount', 'image_storage_dir', 'backup_copy_mount'):
            if config[key] is not None and (not isinstance(config[key], str) or not Path(config[key]).is_absolute() or ',' in config[key]):
                raise Failure(f'{key} must be an absolute path')
        if config['profile'] == 'single-vm-production' and not all(config[k] for k in ('data_mount', 'backup_copy_dir', 'backup_copy_mount')):
            raise Failure('Production requires data_mount, backup_copy_dir and backup_copy_mount')
        if not re.fullmatch(r'[a-z][a-z0-9_-]{1,40}', config['project']):
            raise Failure('Invalid Compose project name')
        if not re.fullmatch(r'\d+\.\d+\.\d+', config['version']):
            raise Failure('Use an exact stable version')
        for key in ['port', 'retention_days', 'command_timeout', 'migration_timeout', 'drain_timeout', 'health_timeout']:
            if type(config[key]) is not int or config[key] < 1:
                raise Failure(f'{key} must be a positive integer')
        if config['port'] > 65535 or type(config['https']) is not bool:
            raise Failure('Invalid port or HTTPS flag')
        site = urlsplit(config['site_url'])
        if site.scheme != ('https' if config['https'] else 'http') or not site.hostname or site.username or site.path not in ('', '/') or site.query or site.fragment:
            raise Failure('site_url must be an HTTP(S) origin matching https')
        if not config['https'] and site.hostname not in ('localhost', '127.0.0.1'):
            raise Failure('Plain HTTP is restricted to localhost; configure a local HTTPS reverse proxy')
        hosts = config['allowed_hosts']
        if not isinstance(hosts, list) or site.hostname not in hosts or any(not isinstance(h, str) or not re.fullmatch(r'[a-zA-Z0-9.-]+', h) for h in hosts):
            raise Failure('allowed_hosts must contain the site hostname and no wildcard')
        for key in ['backup_dir', 'backup_copy_dir']:
            if config[key] is not None and (not isinstance(config[key], str) or not config[key]):
                raise Failure(f'Invalid {key}')
        if not config['backup_dir']:
            raise Failure('backup_dir is required')
        if config['backup_copy_dir'] and not Path(config['backup_copy_dir']).is_absolute():
            raise Failure('backup_copy_dir must be an absolute path')
        hook = config['notification_hook']
        if hook is not None and (not isinstance(hook, list) or not hook or not all(isinstance(x, str) for x in hook) or not Path(hook[0]).is_absolute()):
            raise Failure('notification_hook must be an argv array with an absolute executable')
        for name in ['DD_DATABASE_PASSWORD', 'DD_SECRET_KEY', 'DD_CREDENTIAL_AES_256_KEY', 'DD_ADMIN_PASSWORD', 'DD_JIRA_WEBHOOK_SECRET']:
            if not isinstance(credentials.get(name), str) or not re.fullmatch(r'[A-Za-z0-9_-]{24,}', credentials[name]):
                raise Failure(f'Invalid generated secret: {name}')
        if len(credentials['DD_CREDENTIAL_AES_256_KEY']) != 32:
            raise Failure('AES key must contain exactly 32 characters')

    def release_dir(self, version=None):
        version = version or self.config['version']
        if not re.fullmatch(r'\d+\.\d+\.\d+', version):
            raise Failure('Invalid target version')
        restored = self.state / 'releases' / version
        return restored if restored.exists() else self.root / 'releases' / version

    def phase(self):
        path = self.state / 'status.json'
        return read_json(path) if path.exists() else {'phase': 'new'}

    def set_phase(self, phase, **details):
        previous = self.phase()
        recovery = {key: previous[key] for key in ('backup', 'target', 'previous_metadata') if key in previous}
        if phase in ('ready', 'new'):
            recovery = {}
        write_json(self.state / 'status.json', {'phase': phase, 'project': self.config['project'], 'version': self.config['version'], 'time': dt.datetime.now(dt.timezone.utc).isoformat(), **recovery, **details})

    def redact(self, text):
        for value in sorted(self.secrets.values(), key=len, reverse=True):
            text = text.replace(value, '[REDACTED]')
        return re.sub(r'(postgres(?:ql)?|https?)://[^\s/@]+:[^\s/@]+@', r'\1://[REDACTED]@', text)

    def run(self, args, *, timeout=None, stdin=None, stdout=None):
        env = {k: v for k, v in os.environ.items() if not k.startswith(('COMPOSE_', 'DD_', 'DJANGO_', 'NGINX_'))}
        try:
            process = subprocess.Popen(args, env=env, stdin=stdin, stdout=stdout or subprocess.PIPE,
                                       stderr=subprocess.PIPE, start_new_session=True)
        except OSError as exc:
            raise Failure(f'Cannot execute {args[0]}; check host dependencies and permissions') from exc
        try:
            output, error = process.communicate(timeout=timeout or (self.config or {}).get('command_timeout', 300))
        except BaseException as exc:
            # Kill only local CLI descendants; daemon-managed containers are separate.
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
            if isinstance(exc, subprocess.TimeoutExpired):
                raise Failure('Command timed out; maintenance state retained. Inspect status and container state before retrying') from exc
            raise
        if process.returncode:
            error = self.redact(error.decode(errors='replace'))[-2000:]
            raise Failure(f'Command failed (exit {process.returncode}): {error}')
        return output.decode(errors='replace') if output else ''

    def compose(self, *args, **kwargs):
        return self.run(['docker', 'compose', '--project-name', self.config['project'], '--env-file', '/dev/null', '-f', str(self.state / 'compose.json'), *args], **kwargs)

    def render(self, version=None):
        _, doc = bundle(self.release_dir(version))
        c, s = self.config, self.secrets
        common = {
            'DD_DATABASE_URL': f'postgresql://defectdojo:{s["DD_DATABASE_PASSWORD"]}@postgres:5432/defectdojo',
            'DD_CELERY_BROKER_URL': 'redis://valkey:6379/0', 'DD_CACHE_URL': 'redis://valkey:6379/1',
            'DD_SECRET_KEY': s['DD_SECRET_KEY'], 'DD_CREDENTIAL_AES_256_KEY': s['DD_CREDENTIAL_AES_256_KEY'],
            'DD_DEBUG': 'False', 'DD_ALLOWED_HOSTS': ','.join(c['allowed_hosts']),
            'DD_SITE_URL': c['site_url'], 'DD_CSRF_TRUSTED_ORIGINS': c['site_url'],
            'DD_SESSION_COOKIE_SECURE': str(c['https']), 'DD_CSRF_COOKIE_SECURE': str(c['https']),
            'DD_SECURE_PROXY_SSL_HEADER': str(c['https']),
            'DD_DATABASE_READINESS_TIMEOUT': '120',
            'DD_CELERY_WORKER_POOL_TYPE': 'prefork', 'DD_CELERY_WORKER_CONCURRENCY': str(c['worker_concurrency']),
            'DD_CELERY_WORKER_AUTOSCALE_MIN': str(c['worker_concurrency']), 'DD_CELERY_WORKER_AUTOSCALE_MAX': str(c['worker_concurrency']),
            'DD_CELERY_WORKER_PREFETCH_MULTIPLIER': '1',
            'DD_DATA_UPLOAD_MAX_MEMORY_SIZE': str(c['data_upload_max_memory_mb'] * 1024**2),
        }
        for name, service in doc['services'].items():
            service['restart'] = 'unless-stopped' if name != 'initializer' else 'no'
            service['logging'] = {'driver': 'json-file', 'options': {'max-size': '10m', 'max-file': '3'}}
            if name in c['resource_limits_gib']:
                service['mem_limit'] = f'{c["resource_limits_gib"][name]}g'
            if name in ('uwsgi', 'initializer', 'celerybeat', 'celeryworker'):
                service['environment'] = dict(common)
            if name == 'initializer':
                service['environment'].update({'DD_INITIALIZE': 'true', 'DD_ADMIN_USER': 'admin',
                    'DD_ADMIN_MAIL': 'admin@localhost', 'DD_ADMIN_FIRST_NAME': 'Admin', 'DD_ADMIN_LAST_NAME': 'User',
                    'DD_ADMIN_PASSWORD': s['DD_ADMIN_PASSWORD'], 'DD_JIRA_WEBHOOK_SECRET': s['DD_JIRA_WEBHOOK_SECRET']})
            if name == 'uwsgi':
                service['environment'].update({
                    'DD_UWSGI_NUM_OF_PROCESSES': str(c['uwsgi_processes']),
                    'DD_UWSGI_NUM_OF_THREADS': str(c['uwsgi_threads']),
                    'DD_UWSGI_EXTRA_ARGS': f'--master --stats /tmp/ddm-uwsgi-stats.sock --socket-timeout {c["request_timeout"]}',
                })
            if name == 'postgres':
                service['environment'] = {'PGDATA': '/var/lib/postgresql/18/docker', 'POSTGRES_DB': 'defectdojo',
                                          'POSTGRES_USER': 'defectdojo', 'POSTGRES_PASSWORD': s['DD_DATABASE_PASSWORD']}
                # PostgreSQL 18 declares this parent as VOLUME; bind the named volume there.
                service['volumes'] = [{'type': 'volume', 'source': 'defectdojo_postgres', 'target': '/var/lib/postgresql'}]
                if c['postgres_settings']:
                    service['command'] = ['postgres']
                    for key, value in sorted(c['postgres_settings'].items()):
                        service['command'].extend(['-c', f'{key}={value}'])
                    service['shm_size'] = '1g'
            if name == 'valkey':
                service['command'] = ['valkey-server', '--appendonly', 'yes']
            if name == 'nginx':
                service['environment'] = {'DD_UWSGI_HOST': 'uwsgi', 'DD_UWSGI_PORT': '3031', 'NGINX_METRICS_ENABLED': 'false'}
                service['ports'] = [{'target': 8080, 'published': str(c['port']), 'host_ip': '127.0.0.1', 'protocol': 'tcp'}]
                # The pinned entrypoint has no upload/timeout environment options.
                service['entrypoint'] = ['/bin/sh', '-ec',
                    "sed -i -E 's/client_max_body_size [^;]+;/client_max_body_size " + str(c['upload_max_mb']) + "m;/; "
                    "s/uwsgi_read_timeout [^;]+;/uwsgi_read_timeout " + str(c['request_timeout']) + ";/; "
                    "/^[[:space:]]*client_body_timeout /d; "
                    "/^http \\{/a\\ client_body_timeout " + str(c['upload_timeout']) + "; uwsgi_send_timeout " + str(c['request_timeout']) + ";"
                    "' /etc/nginx/nginx.conf; exec /entrypoint-nginx.sh"]
        generation = read_json(self.state / 'volumes.json')['generation']
        if not re.fullmatch(r'[a-z0-9-]+', generation):
            raise Failure('Invalid volume generation')
        for name, volume in doc['volumes'].items():
            volume['name'] = f'{c["project"]}_{name}_{generation}'
        for network in doc.get('networks', {}).values():
            network.pop('name', None)
        write_json(self.state / 'compose.json', doc)

    def preflight(self, *, check_backup=True):
        log('Checking Docker, Compose, disk space and pinned configuration')
        version = self.run(['docker', 'compose', 'version', '--short']).strip().lstrip('v')
        numbers = tuple(int(x) for x in version.split('.')[:2])
        if numbers < (2, 24):
            raise Failure('Docker Compose 2.24+ is required; install it explicitly')
        info = json.loads(self.run(['docker', 'info', '--format', '{{json .}}']))
        if int(info['ServerVersion'].split('.')[0]) < 25:
            raise Failure('Docker Engine 25+ is required; install it explicitly')
        if info['OSType'] != 'linux' or info['Architecture'] not in ('x86_64', 'amd64'):
            raise Failure('This deployment is validated for Linux amd64 only')
        self.storage_check(info=info, check_backup=check_backup)
        self.compose('config', '--quiet')

    def filesystem(self, path):
        # Mountinfo avoids traversing root-owned Docker directories to find their filesystem.
        path = Path(path).absolute()
        mounts = []
        for line in Path('/proc/self/mountinfo').read_text().splitlines():
            fields = line.split()
            mount = Path(re.sub(r'\\([0-7]{3})', lambda m: chr(int(m[1], 8)), fields[4]))
            if path.is_relative_to(mount):
                mounts.append((mount, fields[fields.index('-') + 1]))
        if not mounts:
            raise Failure(f'Cannot identify filesystem for {path}')
        return max(mounts, key=lambda item: len(item[0].parts))

    def require_space(self, demands):
        totals = {}
        for path, amount in demands:
            mount, _ = self.filesystem(path)
            device = mount.stat().st_dev
            previous = totals.get(device, (mount, 0))
            totals[device] = (mount, previous[1] + amount)
        reserve = self.config['disk_reserve_gib'] * GIB
        for mount, needed in totals.values():
            free = shutil.disk_usage(mount).free
            log(f'Disk {mount}: free={free / GIB:.1f} GiB, additional={needed / GIB:.1f} GiB, reserve={reserve / GIB} GiB')
            if free < needed + reserve:
                raise Failure(f'Insufficient free space on {mount}; need additional data plus reserve, without deleting old generations')

    def backup_target(self):
        destination = self.config['backup_copy_dir']
        if not destination:
            return None
        target = Path(destination).resolve()
        expected = self.config['backup_copy_mount']
        if not expected or not os.path.ismount(expected) or self.filesystem(target)[0] != Path(expected).resolve():
            raise Failure('Separate backup mount is missing or does not contain backup_copy_dir; refusing local fallback')
        if not target.is_dir() or target.stat().st_mode & 0o077 or target.is_relative_to(self.backup_root()):
            raise Failure('Separate backup destination must already exist with mode 0700, distinct from backup_dir')
        if self.config['profile'] == 'single-vm-production' and self.filesystem(target)[1] not in ('nfs', 'nfs4', 'cifs', 'fuse.sshfs', 'ceph', 'fuse.glusterfs'):
            raise Failure('Production backup_copy_mount must be a remote filesystem outside this VM')
        return target

    def storage_check(self, *, info=None, operation=None, backup=None, check_backup=True):
        # Paths below refer to this host. A remote daemon would invalidate these checks.
        endpoint = None if os.environ.get('DOCKER_CONTEXT') else os.environ.get('DOCKER_HOST')
        if not endpoint:
            context = json.loads(self.run(['docker', 'context', 'inspect']))
            endpoint = context[0]['Endpoints']['docker']['Host']
        if not endpoint.startswith('unix://'):
            raise Failure('Storage checks require a local Docker daemon over a Unix socket')
        info = info or json.loads(self.run(['docker', 'info', '--format', '{{json .}}']))
        docker_root = Path(info['DockerRootDir']).resolve()
        demands = [(self.state, 0), (docker_root, 0)]
        data_mount = self.config['data_mount']
        if data_mount:
            if not os.path.ismount(data_mount):
                raise Failure('data_mount is not mounted; refusing system-disk fallback')
            if self.filesystem(docker_root)[0] != Path(data_mount).resolve():
                raise Failure('DockerRootDir is not on data_mount')
        image_dir = self.config['image_storage_dir']
        snapshotter = any('io.containerd.snapshotter' in str(row) for row in info.get('DriverStatus', []))
        if snapshotter and not image_dir:
            raise Failure('Set image_storage_dir to the verified containerd image-store path')
        if image_dir:
            image_dir = Path(image_dir).resolve()
            if not image_dir.is_dir() or (data_mount and self.filesystem(image_dir)[0] != Path(data_mount).resolve()):
                raise Failure('Image storage directory is missing or outside data_mount')
            demands.append((image_dir, 0))
        names = self.run(['docker', 'volume', 'ls', '--format', '{{.Name}}']).split()
        doc = read_json(self.state / 'compose.json')
        for service, definition in doc['services'].items():
            for cid in self.compose('ps', '-a', '-q', service).split():
                mounts = json.loads(self.run(['docker', 'inspect', '--format', '{{json .Mounts}}', cid]))
                for expected in definition.get('volumes', []):
                    if expected['type'] != 'volume':
                        continue
                    name = doc['volumes'][expected['source']]['name']
                    if not any(m.get('Type') == 'volume' and m.get('Name') == name and m.get('Destination') == expected['target'] for m in mounts):
                        raise Failure(f'Unexpected live mount for {service}:{expected["target"]}; inspect before maintenance')
        data_bytes = 0
        data_paths = [docker_root]
        for key in ('defectdojo_postgres', 'defectdojo_media', 'defectdojo_redis'):
            name = doc['volumes'][key]['name']
            if name not in names:
                continue
            volume = json.loads(self.run(['docker', 'volume', 'inspect', name]))[0]
            path = Path(volume['Mountpoint']).absolute()
            log(f'Volume {name}: {path}')
            if data_mount and self.filesystem(path)[0] != Path(data_mount).resolve():
                raise Failure(f'Volume {name} is outside data_mount')
            demands.append((path, 0))
            data_paths.append(path)
            if key != 'defectdojo_redis':
                image = doc['services']['postgres']['image']
                size = self.run(['docker', 'run', '--rm', '--pull', 'never', '--network', 'none', '--read-only',
                                 '--mount', f'type=volume,source={name},target=/probe,readonly',
                                 '--entrypoint', 'du', image, '-sk', '/probe'], timeout=self.config['backup_timeout'])
                data_bytes += int(size.split()[0]) * 1024
        root = self.backup_root()
        target = self.backup_target() if check_backup and operation != 'restore' else None
        if check_backup and operation != 'restore':
            demands.append((root, 0))
        if target:
            demands.append((target, 0))
        estimate = max(data_bytes, self.config['backup_estimate_gib'] * GIB)
        if operation == 'backup':
            demands.append((root, estimate))
            if target:
                demands.append((target, estimate))
        elif operation == 'restore':
            demands.append((backup, 0))
            manifest = read_json(backup / 'manifest.json')
            # Raw source size includes indexes; WAL/index construction needs additional headroom.
            restored = max(data_bytes, manifest.get('source_data_bytes', 0), self.config['restore_estimate_gib'] * GIB)
            archive = sum(p.stat().st_size for p in backup.rglob('*') if p.is_file())
            restored = max(restored, archive * 4)
            mounts = {self.filesystem(path)[0] for path in data_paths}
            devices = {mount.stat().st_dev: mount for mount in mounts}
            # Each distinct data filesystem must fit a full new generation conservatively.
            demands.extend((mount, restored * 3 // 2) for mount in devices.values())
        self.require_space(demands)
        return data_bytes

    def begin_maintenance(self, action):
        path = self.state / 'maintenance.json'
        if not path.exists() or read_json(path).get('finished'):
            write_json(path, {'action': action, 'started': dt.datetime.now(dt.timezone.utc).isoformat()})

    def report_maintenance(self, *, finished=False, persist=True):
        path = self.state / 'maintenance.json'
        if not path.exists():
            return
        report = read_json(path)
        if report.get('finished'):
            return
        now = dt.datetime.now(dt.timezone.utc)
        report['elapsed_seconds'] = (now - dt.datetime.fromisoformat(report['started'])).total_seconds()
        if finished:
            report['finished'] = now.isoformat()
        if persist:
            write_json(path, report)
        log(f'Maintenance ingress downtime: {report["elapsed_seconds"]:.1f}s; ' + ('ready' if finished else 'ongoing, ingress recovery not confirmed'))

    def pull(self, version=None):
        manifest, _ = bundle(self.release_dir(version))
        for image in sorted(set(manifest['images'].values())):
            log(f'Pulling pinned image {image}')
            self.run(['docker', 'pull', image], timeout=max(900, self.config['command_timeout']))

    def stop_service(self, service, graceful_signal='SIGTERM'):
        ids = self.compose('ps', '-q', service).split()
        if ids:
            if service == 'uwsgi':
                # HTTP clients may have timed out while synchronous imports still run.
                probe = """
import json, socket
with socket.socket(socket.AF_UNIX) as s:
    s.settimeout(10)
    s.connect('/tmp/ddm-uwsgi-stats.sock')
    with s.makefile('rb') as stream:
        workers = json.load(stream)['workers']
assert workers and all(w.get('cores') for w in workers)
print(sum(c['in_request'] for w in workers for c in w['cores']))
"""
                deadline = time.monotonic() + self.config['drain_timeout']
                while time.monotonic() < deadline:
                    if int(self.compose('exec', '-T', 'uwsgi', 'python', '-c', probe).strip()) == 0:
                        break
                    time.sleep(2)
                else:
                    raise Failure('HTTP imports are still running; uWSGI was not stopped')
                graceful_signal = 'SIGINT'
            # No daemon-side SIGKILL: timing out only stops the waiting Docker client.
            self.run(['docker', 'stop', '--signal', graceful_signal, '--time', '-1', *ids], timeout=self.config['drain_timeout'])

    def shell(self, script, *, oneoff=False):
        args = ['run', '--rm', '--no-deps', '-T', '--entrypoint', 'python', 'uwsgi'] if oneoff else ['exec', '-T', 'uwsgi', 'python']
        output = self.compose(*args, 'manage.py', 'shell', '-c', script, timeout=45)
        results = [line.removeprefix('DD_RESULT=') for line in output.splitlines() if line.startswith('DD_RESULT=')]
        if not results:
            raise Failure('Application probe did not return a result')
        return json.loads(results[-1])

    def quiesce(self):
        self.require_no_initializer()
        self.begin_maintenance('drain')
        self.set_phase('maintenance', action='draining')
        log('Closing ingress gracefully and stopping job producers; waiting for imports and Celery queues')
        self.stop_service('nginx', 'SIGQUIT')
        self.stop_service('celerybeat')
        self.stop_service('uwsgi', 'SIGINT')
        deadline = time.monotonic() + self.config['drain_timeout']
        idle = 0
        while time.monotonic() < deadline:
            try:
                pending = self.shell(BROKER_CHECK, oneoff=True)
                if self.compose('ps', '-q', 'celeryworker').strip():
                    pending += self.shell(WORK_CHECK, oneoff=True)
                idle = idle + 1 if pending == 0 else 0
                if idle >= 2:
                    break
            except Failure:
                idle = 0
            time.sleep(2)
        else:
            raise Failure('Drain timed out; no jobs purged or force-killed. Ingress/beat remain stopped; inspect worker then start --resume')
        self.stop_service('celeryworker')
        if self.shell(BROKER_CHECK, oneoff=True):
            raise Failure('Broker still contains work; backup/upgrade refused')
        self.set_phase('maintenance', action='quiesced')

    def migrate(self):
        log('Waiting for official initializer and all migrations')
        ids = self.compose('ps', '-q', 'initializer').split()
        if ids:
            expected = read_json(self.state / 'compose.json')['services']['initializer']['image']
            if self.run(['docker', 'inspect', '--format', '{{.Config.Image}}', ids[0]]).strip() != expected:
                raise Failure('A different initializer is still running; wait and inspect before recovery')
            log('Resuming wait for existing initializer; it will not be interrupted')
        else:
            self.compose('up', '-d', '--no-build', '--pull', 'never', 'postgres', 'valkey')
            self.compose('up', '-d', '--no-build', '--pull', 'never', '--no-deps', '--force-recreate', 'initializer')
            ids = self.compose('ps', '-a', '-q', 'initializer').split()
        if len(ids) != 1:
            raise Failure('Expected exactly one initializer')
        result = self.run(['docker', 'wait', ids[0]], timeout=self.config['migration_timeout']).strip()
        if result != '0':
            raise Failure('Initializer failed; migration may be partial. Review private initializer logs; retry same target or explicit restore')

    def require_no_initializer(self):
        if self.compose('ps', '-q', 'initializer').strip():
            raise Failure('Initializer is still running; let it finish before backup, stop, upgrade or restore')

    def http_check(self, frontend=False):
        server = 'nginx:8080' if frontend else '127.0.0.1:8081'
        host = urlsplit(self.config['site_url']).netloc
        probe = "import urllib.request; r=urllib.request.urlopen(urllib.request.Request('http://" + server + "/login', headers={'Host': " + repr(host) + "}),timeout=10); assert r.status == 200"
        self.compose('exec', '-T', 'uwsgi', 'python', '-c', probe)

    def health(self, frontend=False):
        log('Checking PostgreSQL, Valkey, Django migrations, worker, scheduler and HTTP')
        self.compose('exec', '-T', 'postgres', 'pg_isready', '-U', 'defectdojo', '-d', 'defectdojo')
        if self.compose('exec', '-T', 'valkey', 'valkey-cli', 'ping').strip() != 'PONG':
            raise Failure('Valkey did not return PONG')
        self.compose('exec', '-T', 'uwsgi', 'python', 'manage.py', 'check')
        self.compose('exec', '-T', 'uwsgi', 'python', 'manage.py', 'migrate', '--check')
        ping = "from celery import current_app; import json; print('DD_RESULT=' + json.dumps(current_app.control.ping(timeout=5)))"
        replies = self.shell(ping)
        if not replies or not all(response.get('ok') == 'pong' for reply in replies for response in reply.values()):
            raise Failure('No Celery worker ping reply')
        ids = self.compose('ps', '-q', 'celerybeat').split()
        if len(ids) != 1:
            raise Failure('Celery scheduler is not running')
        self.http_check(frontend=frontend)

    def runtime_settings(self):
        c = self.config
        nginx = self.compose('exec', '-T', 'nginx', 'nginx', '-T')
        for directive, value in {'client_max_body_size': f'{c["upload_max_mb"]}m',
                                 'uwsgi_read_timeout': str(c['request_timeout']),
                                 'uwsgi_send_timeout': str(c['request_timeout']),
                                 'client_body_timeout': str(c['upload_timeout'])}.items():
            actual = re.findall(r'\b' + directive + r'\s+([^;]+);', nginx)
            if not actual or any(v.strip() != value for v in actual):
                raise Failure(f'Container nginx setting differs from config: {directive}')
        actual = self.shell("""
import json, socket
from django.conf import settings
with socket.socket(socket.AF_UNIX) as s:
    s.settimeout(10)
    s.connect('/tmp/ddm-uwsgi-stats.sock')
    with s.makefile('rb') as stream:
        workers = json.load(stream)['workers']
print('DD_RESULT=' + json.dumps({'upload': settings.DATA_UPLOAD_MAX_MEMORY_SIZE,
    'processes': len(workers), 'threads': min(len(w['cores']) for w in workers)}))
""")
        if actual != {'upload': c['data_upload_max_memory_mb'] * 1024**2,
                       'processes': c['uwsgi_processes'], 'threads': c['uwsgi_threads']}:
            raise Failure('Effective application/uWSGI settings differ from config; apply with controlled start')
        conditions = []
        for key, value in c['postgres_settings'].items():
            current = f"current_setting('{key}')"
            conditions.append(f"pg_size_bytes({current}) = pg_size_bytes('{value}')" if key in PG_MEMORY else f"{current}::int = {value}")
        if conditions:
            result = self.compose('exec', '-T', 'postgres', 'psql', '-U', 'defectdojo', '-d', 'defectdojo', '-Atc', 'SELECT ' + ' AND '.join(conditions))
            if result.strip() != 't':
                raise Failure('Effective PostgreSQL settings differ from config')
        log(f'Container/application settings verified. Check host proxy separately: body={c["upload_max_mb"]}m, read/send={c["request_timeout"]}s, body timeout={c["upload_timeout"]}s')

    def activate(self):
        if any(self.compose('ps', '-q', service).strip() for service in WRITERS):
            self.quiesce()
        self.set_phase('migrating')
        try:
            self.migrate()
            self.compose('up', '-d', '--no-build', '--pull', 'never', '--no-deps', 'uwsgi', 'celeryworker')
            # Beat is started for validation; ingress remains closed until all probes pass.
            self.compose('up', '-d', '--no-build', '--pull', 'never', '--no-deps', 'celerybeat')
            deadline = time.monotonic() + self.config['health_timeout']
            while True:
                try:
                    self.health()
                    break
                except Failure:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(3)
            self.compose('up', '-d', '--no-build', '--pull', 'never', '--no-deps', 'nginx')
            deadline = time.monotonic() + self.config['health_timeout']
            while True:
                try:
                    self.http_check(frontend=True)
                    break
                except Failure:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(3)
        except BaseException:
            self.set_phase('failed', action='activation', recovery='start --resume retries the active target; restore requires explicit confirmation')
            for service in WRITERS:
                try:
                    self.stop_service(service, 'SIGQUIT' if service == 'nginx' else 'SIGTERM')
                except Failure:
                    log(f'Could not confirm {service} stopped; inspect containers before recovery')
            raise
        self.set_phase('ready')
        self.report_maintenance(finished=True)
        log(f'DefectDojo {self.config["version"]} is ready at {self.config["site_url"]}')

    def install(self):
        self.load()
        if self.phase()['phase'] != 'new':
            raise Failure('Install requires new state; use start or start --resume for an existing deployment')
        self.render()
        self.preflight()
        self.pull()
        self.activate()

    def backup_root(self):
        return (self.root / self.config['backup_dir']).resolve()

    def backup(self, *, quiesced=False, upgrade=False):
        data_bytes = self.storage_check(operation='backup')
        if not quiesced:
            self.begin_maintenance('backup')
            self.quiesce()
            data_bytes = self.storage_check(operation='backup')
        root = self.backup_root()
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if root.stat().st_mode & 0o077:
            raise Failure('Backup directory must have mode 0700')
        name = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') + ('-upgrade' if upgrade else '-daily')
        temp = root / ('.partial-' + name)
        temp.mkdir(mode=0o700)
        log('Creating quiesced PostgreSQL and media backup')
        try:
            with (temp / 'database.dump').open('wb') as output:
                self.compose('exec', '-T', 'postgres', 'pg_dump', '-U', 'defectdojo', '-d', 'defectdojo', '--format=custom', '--no-owner', stdout=output, timeout=self.config['backup_timeout'])
            with (temp / 'media.tar.gz').open('wb') as output:
                self.compose('run', '--rm', '--no-deps', '-T', '--entrypoint', 'tar', 'uwsgi', '-C', '/app/media', '-czf', '-', '.', stdout=output, timeout=self.config['backup_timeout'])
            for name_ in ['config.json', 'secrets.json', 'compose.json', 'volumes.json']:
                shutil.copyfile(self.state / name_, temp / name_)
            shutil.copytree(self.release_dir(), temp / 'bundle')
            release, _ = bundle(self.release_dir())
            images = {}
            for image in sorted(set(release['images'].values())):
                images[image] = json.loads(self.run(['docker', 'image', 'inspect', image]))[0]
                images[image] = {k: images[image][k] for k in ('Id', 'RepoDigests')}
            files = {str(p.relative_to(temp)): sha256(p) for p in sorted(temp.rglob('*')) if p.is_file()}
            write_json(temp / 'manifest.json', {'format': 1, 'version': self.config['version'], 'created': dt.datetime.now(dt.timezone.utc).isoformat(),
                'images': release['images'], 'image_inspect': images, 'files': files, 'kind': 'upgrade' if upgrade else 'daily', 'source_data_bytes': data_bytes})
            for path in temp.rglob('*'):
                path.chmod(0o700 if path.is_dir() else 0o600)
            verify_backup(temp)
            self.validate_dump(temp)
            final = root / name
            temp.rename(final)
            self.copy_backup(final)
            self.retain()
        except BaseException:
            log(f'Backup failed; partial files may remain in {temp}. Ingress remains closed; upgrade blocked')
            raise
        log(f'Backup verified: {final}')
        if not quiesced:
            self.activate()
        return final

    def validate_dump(self, backup, *, restoring=False):
        manifest = read_json(backup / 'bundle/release.json')
        with (backup / 'database.dump').open('rb') as source:
            self.run(['docker', 'run', '--rm', '-i', '--network', 'none', '--entrypoint', 'pg_restore', manifest['images']['postgres'], '--list'], stdin=source, timeout=self.config['restore_timeout' if restoring else 'backup_timeout'])

    def copy_backup(self, backup):
        destination = self.config['backup_copy_dir']
        if not destination:
            return
        target = self.backup_target()
        temp = target / ('.partial-' + backup.name)
        shutil.copytree(backup, temp)
        self.backup_target()
        verify_backup(temp)
        temp.rename(target / backup.name)
        log('Separate backup copy verified; remote retention is operator-managed')

    def retain(self):
        cutoff = time.time() - self.config['retention_days'] * 86400
        candidates = sorted(self.backup_root().glob('*-daily'))
        # Always retain the newest successful daily backup and every upgrade backup.
        for path in candidates[:-1]:
            if path.is_dir() and not path.is_symlink() and path.stat().st_mtime < cutoff:
                verify_backup(path)
                shutil.rmtree(path)

    def upgrade_edge(self, target):
        policy = read_json(self.root / 'releases/upgrade-paths.json')
        for edge in policy['paths']:
            if edge['from'] == self.config['version'] and edge['to'] == target:
                source, _ = bundle(self.release_dir())
                dest, _ = bundle(self.release_dir(target))
                if edge.get('tested') and edge.get('notes') and edge.get('evidence') and edge.get('from_commit') == source['commit'] and edge.get('to_commit') == dest['commit']:
                    if source['images']['postgres'] != dest['images']['postgres'] or source['images']['valkey'] != dest['images']['valkey']:
                        raise Failure('Database/broker image changes require a dedicated reviewed data migration')
                    return edge
        raise Failure(f'Unverified upgrade path {self.config["version"]} -> {target}; review all intermediate release notes and rehearse before adding a policy edge')

    def upgrade(self, target):
        self.upgrade_edge(target)
        if self.phase()['phase'] != 'ready':
            raise Failure('Upgrade requires a ready deployment')
        self.preflight()
        self.pull(target)
        self.storage_check(operation='backup')
        self.begin_maintenance('upgrade')
        self.quiesce()
        backup = self.backup(quiesced=True, upgrade=True)
        self.set_phase('maintenance', action='upgrade', backup=str(backup), target=target)
        self.config['version'] = target
        write_json(self.state / 'config.json', self.config)
        self.render()
        self.activate()

    def restore(self, backup, *, confirm):
        expected = f'RESTORE {self.config["project"]}'
        if confirm != expected:
            raise Failure(f'Restore replaces the active database/media/configuration. Pass --confirm "{expected}" after reviewing the backup')
        manifest = verify_backup(backup)
        log(f'Validating restore of {manifest["version"]}; existing volumes will be preserved')
        self.validate_dump(backup, restoring=True)
        old_config = dict(self.config)
        # Validate restored configuration before stopping any current service.
        saved = read_json(backup / 'config.json')
        if saved['version'] != manifest['version']:
            raise Failure('Restored config version differs from manifest')
        self.validate_config(saved, read_json(backup / 'secrets.json'))
        self.require_no_initializer()
        self.storage_check(operation='restore', backup=backup)
        for image in sorted(set(manifest['images'].values())):
            self.run(['docker', 'pull', image], timeout=max(900, self.config['command_timeout']))
        previous = self.state / 'history' / dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        previous.mkdir(parents=True, mode=0o700)
        for name in ('config.json', 'secrets.json', 'compose.json', 'volumes.json', 'status.json'):
            if (self.state / name).exists():
                shutil.copyfile(self.state / name, previous / name)
                (previous / name).chmod(0o600)
        self.set_phase('restoring', backup=str(backup), previous_metadata=str(previous))
        self.begin_maintenance('restore')
        for service in WRITERS:
            self.stop_service(service, 'SIGQUIT' if service == 'nginx' else 'SIGTERM')
        self.stop_service('postgres')
        self.stop_service('valkey')
        self.compose('down', '--timeout', str(self.config['drain_timeout']))
        release_path = self.state / 'releases' / manifest['version']
        release_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if release_path.exists():
            shutil.rmtree(release_path)
        shutil.copytree(backup / 'bundle', release_path)
        for key in set(OPTIONAL_DEFAULTS) | {'project', 'port', 'site_url', 'allowed_hosts', 'https', 'backup_dir', 'backup_copy_dir', 'notification_hook', 'command_timeout', 'migration_timeout', 'drain_timeout', 'health_timeout'}:
            saved[key] = old_config[key]
        write_json(self.state / 'config.json', saved)
        write_json(self.state / 'secrets.json', read_json(backup / 'secrets.json'))
        write_json(self.state / 'volumes.json', {'generation': 'restore-' + secrets.token_hex(8)})
        self.load()
        self.render()
        self.pull()
        self.compose('up', '-d', '--no-build', '--pull', 'never', 'postgres', 'valkey')
        deadline = time.monotonic() + self.config['health_timeout']
        while True:
            try:
                self.compose('exec', '-T', 'postgres', 'pg_isready', '-U', 'defectdojo', '-d', 'defectdojo')
                break
            except Failure:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(2)
        with (backup / 'database.dump').open('rb') as source:
            self.compose('exec', '-T', 'postgres', 'pg_restore', '-U', 'defectdojo', '-d', 'defectdojo', '--exit-on-error', '--no-owner', stdin=source, timeout=self.config['restore_timeout'])
        with (backup / 'media.tar.gz').open('rb') as source:
            self.compose('run', '--rm', '--no-deps', '-T', '--user', '0', '--entrypoint', 'tar', 'uwsgi', '-C', '/app/media', '-xzf', '-', stdin=source, timeout=self.config['restore_timeout'])
        self.activate()
        log('Restore complete; previous volume generations retained for manual recovery')

    def check_update(self):
        request = urllib.request.Request('https://api.github.com/repos/DefectDojo/django-DefectDojo/releases/latest', headers={'Accept': 'application/vnd.github+json', 'User-Agent': 'defectdojo-update-manager'})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                release = json.load(response)
        except (OSError, ValueError) as exc:
            raise Failure('Release check failed; no deployment changes made') from exc
        tag = release['tag_name']
        if release.get('draft') or release.get('prerelease') or not re.fullmatch(r'\d+\.\d+\.\d+', tag):
            raise Failure('Latest release response is not stable')
        log(f'Installed: {self.config["version"]}; latest stable: {tag}; https://github.com/DefectDojo/django-DefectDojo/releases/tag/{tag}. No upgrade performed')

    def notify(self, result):
        hook = (self.config or {}).get('notification_hook')
        if hook:
            try:
                self.run([*hook, result], timeout=15)
            except Failure:
                log('Notification hook failed; see journal')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', type=Path, default=ROOT / '.runtime')
    parser.add_argument('--dry-run', action='store_true', help='Print intent only; no writes, Docker calls, network or timer changes')
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ['init', 'install', 'stop', 'status', 'doctor', 'backup', 'check-update']:
        commands.add_parser(name)
    start = commands.add_parser('start')
    start.add_argument('--resume', action='store_true', help='Explicitly retry the active version after failed maintenance')
    upgrade = commands.add_parser('upgrade')
    upgrade.add_argument('target')
    restore = commands.add_parser('restore')
    restore.add_argument('backup', type=Path)
    restore.add_argument('--confirm')
    args = parser.parse_args(argv)
    if args.dry_run:
        log(f'DRY RUN: {args.command}; state={args.state}; no operations executed; prerequisites and runtime health not checked')
        return 0
    manager = Manager(ROOT, args.state)
    os.umask(0o077)
    try:
        with manager.lock():
            if args.command == 'init':
                manager.init()
            else:
                manager.load()
                if args.command == 'status':
                    manager.report_maintenance()
                    log(json.dumps(manager.phase()))
                    if (manager.state / 'compose.json').exists():
                        log(manager.compose('ps', '-a'))
                    return 0 if manager.phase()['phase'] == 'ready' else 3
                if args.command == 'check-update':
                    manager.check_update()
                elif args.command == 'install':
                    manager.install()
                else:
                    if args.command not in ('restore',) or not (manager.state / 'compose.json').exists():
                        manager.render()
                    if args.command == 'doctor':
                        manager.preflight()
                        if manager.phase()['phase'] != 'new':
                            manager.health(frontend=True)
                            manager.runtime_settings()
                        else:
                            log('Host and configuration checks passed; application is not installed yet')
                            return 0
                        if manager.phase()['phase'] != 'ready':
                            raise Failure('Maintenance is incomplete; inspect status then start --resume')
                    elif args.command == 'start':
                        if manager.phase()['phase'] == 'restoring':
                            raise Failure('Restore is incomplete; rerun the explicit restore command, do not start a partial database')
                        if manager.phase()['phase'] not in ('ready', 'stopped') and not args.resume:
                            raise Failure('Incomplete maintenance; inspect status then explicitly use start --resume')
                        manager.preflight(check_backup=False)
                        if manager.phase()['phase'] in ('ready', 'maintenance'):
                            manager.quiesce()
                        manager.activate()
                    elif args.command == 'stop':
                        if manager.phase()['phase'] != 'stopped':
                            manager.quiesce()
                            manager.stop_service('postgres')
                            manager.stop_service('valkey')
                            manager.set_phase('stopped')
                        log('Deployment is stopped; persistent volumes retained')
                    elif args.command == 'backup':
                        if manager.phase()['phase'] != 'ready':
                            raise Failure('Backup requires a ready deployment; recover incomplete maintenance first')
                        manager.backup()
                    elif args.command == 'upgrade':
                        manager.upgrade(args.target)
                    elif args.command == 'restore':
                        manager.restore(args.backup.resolve(), confirm=args.confirm)
            manager.notify(f'{args.command}:success')
    except (Failure, OSError, ValueError, KeyError, TypeError) as exc:
        log('ERROR: ' + manager.redact(str(exc)))
        # The lock may have been refused; never mutate another operation's report here.
        with contextlib.suppress(Failure, OSError, ValueError, KeyError, TypeError):
            manager.report_maintenance(persist=False)
        manager.notify(f'{args.command}:failure')
        return 1
    except KeyboardInterrupt:
        with contextlib.suppress(Failure, OSError, ValueError, KeyError, TypeError):
            manager.report_maintenance(persist=False)
        log('Interrupted; inspect status before resuming. No automatic restore performed')
        return 130
    return 0


if __name__ == '__main__':
    sys.exit(main())
