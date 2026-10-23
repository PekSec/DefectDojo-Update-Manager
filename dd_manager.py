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
        if set(config) != set(defaults):
            raise Failure('Configuration keys must match config.example.json')
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
            'DD_CELERY_WORKER_POOL_TYPE': 'prefork', 'DD_CELERY_WORKER_CONCURRENCY': '2',
            'DD_CELERY_WORKER_AUTOSCALE_MIN': '2', 'DD_CELERY_WORKER_AUTOSCALE_MAX': '2',
            'DD_CELERY_WORKER_PREFETCH_MULTIPLIER': '1',
        }
        for name, service in doc['services'].items():
            service['restart'] = 'unless-stopped' if name != 'initializer' else 'no'
            service['logging'] = {'driver': 'json-file', 'options': {'max-size': '10m', 'max-file': '3'}}
            if name in ('uwsgi', 'initializer', 'celerybeat', 'celeryworker'):
                service['environment'] = dict(common)
            if name == 'initializer':
                service['environment'].update({'DD_INITIALIZE': 'true', 'DD_ADMIN_USER': 'admin',
                    'DD_ADMIN_MAIL': 'admin@localhost', 'DD_ADMIN_FIRST_NAME': 'Admin', 'DD_ADMIN_LAST_NAME': 'User',
                    'DD_ADMIN_PASSWORD': s['DD_ADMIN_PASSWORD'], 'DD_JIRA_WEBHOOK_SECRET': s['DD_JIRA_WEBHOOK_SECRET']})
            if name == 'uwsgi':
                service['environment']['DD_UWSGI_EXTRA_ARGS'] = '--master --stats /tmp/ddm-uwsgi-stats.sock'
            if name == 'postgres':
                service['environment'] = {'PGDATA': '/var/lib/postgresql/18/docker', 'POSTGRES_DB': 'defectdojo',
                                          'POSTGRES_USER': 'defectdojo', 'POSTGRES_PASSWORD': s['DD_DATABASE_PASSWORD']}
                # PostgreSQL 18 declares this parent as VOLUME; bind the named volume there.
                service['volumes'] = [{'type': 'volume', 'source': 'defectdojo_postgres', 'target': '/var/lib/postgresql'}]
            if name == 'valkey':
                service['command'] = ['valkey-server', '--appendonly', 'yes']
            if name == 'nginx':
                service['environment'] = {'DD_UWSGI_HOST': 'uwsgi', 'DD_UWSGI_PORT': '3031', 'NGINX_METRICS_ENABLED': 'false'}
                service['ports'] = [{'target': 8080, 'published': str(c['port']), 'host_ip': '127.0.0.1', 'protocol': 'tcp'}]
        generation = read_json(self.state / 'volumes.json')['generation']
        if not re.fullmatch(r'[a-z0-9-]+', generation):
            raise Failure('Invalid volume generation')
        for name, volume in doc['volumes'].items():
            volume['name'] = f'{c["project"]}_{name}_{generation}'
        for network in doc.get('networks', {}).values():
            network.pop('name', None)
        write_json(self.state / 'compose.json', doc)

    def preflight(self):
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
        if shutil.disk_usage(self.state).free < 5 * 1024**3:
            raise Failure('Less than 5 GiB free; provision space for images, database and multiple backups')
        self.compose('config', '--quiet')

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
        if not quiesced:
            self.quiesce()
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
                self.compose('exec', '-T', 'postgres', 'pg_dump', '-U', 'defectdojo', '-d', 'defectdojo', '--format=custom', '--no-owner', stdout=output)
            with (temp / 'media.tar.gz').open('wb') as output:
                self.compose('run', '--rm', '--no-deps', '-T', '--entrypoint', 'tar', 'uwsgi', '-C', '/app/media', '-czf', '-', '.', stdout=output)
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
                'images': release['images'], 'image_inspect': images, 'files': files, 'kind': 'upgrade' if upgrade else 'daily'})
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

    def validate_dump(self, backup):
        manifest = read_json(backup / 'bundle/release.json')
        with (backup / 'database.dump').open('rb') as source:
            self.run(['docker', 'run', '--rm', '-i', '--network', 'none', '--entrypoint', 'pg_restore', manifest['images']['postgres'], '--list'], stdin=source)

    def copy_backup(self, backup):
        destination = self.config['backup_copy_dir']
        if not destination:
            return
        target = Path(destination).resolve()
        if not target.is_dir() or target.stat().st_mode & 0o077 or target.is_relative_to(self.backup_root()):
            raise Failure('Separate backup destination must already exist with mode 0700, distinct from backup_dir')
        temp = target / ('.partial-' + backup.name)
        shutil.copytree(backup, temp)
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
        self.validate_dump(backup)
        old_config = dict(self.config)
        # Validate restored configuration before stopping any current service.
        saved = read_json(backup / 'config.json')
        if saved['version'] != manifest['version']:
            raise Failure('Restored config version differs from manifest')
        self.validate_config(saved, read_json(backup / 'secrets.json'))
        self.require_no_initializer()
        for image in sorted(set(manifest['images'].values())):
            self.run(['docker', 'pull', image], timeout=max(900, self.config['command_timeout']))
        previous = self.state / 'history' / dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        previous.mkdir(parents=True, mode=0o700)
        for name in ('config.json', 'secrets.json', 'compose.json', 'volumes.json', 'status.json'):
            if (self.state / name).exists():
                shutil.copyfile(self.state / name, previous / name)
                (previous / name).chmod(0o600)
        self.set_phase('restoring', backup=str(backup), previous_metadata=str(previous))
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
        for key in ['project', 'port', 'site_url', 'allowed_hosts', 'https', 'backup_dir', 'backup_copy_dir', 'notification_hook']:
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
            self.compose('exec', '-T', 'postgres', 'pg_restore', '-U', 'defectdojo', '-d', 'defectdojo', '--exit-on-error', '--no-owner', stdin=source)
        with (backup / 'media.tar.gz').open('rb') as source:
            self.compose('run', '--rm', '--no-deps', '-T', '--user', '0', '--entrypoint', 'tar', 'uwsgi', '-C', '/app/media', '-xzf', '-', stdin=source)
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
                        manager.preflight()
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
        manager.notify(f'{args.command}:failure')
        return 1
    except KeyboardInterrupt:
        log('Interrupted; inspect status before resuming. No automatic restore performed')
        return 130
    return 0


if __name__ == '__main__':
    sys.exit(main())
