"""Fetch only official deployment files and lock registry digests; never deploy."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dd_manager import ROOT, SERVICES


API = 'https://api.github.com/repos/DefectDojo/django-DefectDojo'


def fetch(url, headers=None):
    request = urllib.request.Request(url, headers={'User-Agent': 'defectdojo-update-manager', **(headers or {})})
    with urllib.request.urlopen(request, timeout=45) as response:
        return response.read()


def deployment(doc, images):
    if set(doc['services']) != SERVICES:
        raise ValueError('New service topology requires an architecture review')
    doc.pop('name', None)
    for name, service in doc['services'].items():
        service.pop('build', None)
        service['volumes'] = [v for v in service.get('volumes', []) if v['type'] != 'bind']
        if name in ('nginx', 'uwsgi', 'initializer', 'celeryworker', 'celerybeat'):
            service['image'] = images['defectdojo-nginx' if name == 'nginx' else 'defectdojo-django']
        service['environment'] = {}
        service.pop('ports', None)
    for volume in doc['volumes'].values():
        volume.pop('name', None)
    for network in doc.get('networks', {}).values():
        network.pop('name', None)
    return doc


def vendor(version, commit):
    if not re.fullmatch(r'\d+\.\d+\.\d+', version) or not re.fullmatch(r'[0-9a-f]{40}', commit):
        raise ValueError('Specify an exact stable VERSION and full COMMIT')
    release = json.loads(fetch(API + '/releases/tags/' + version))
    if release['draft'] or release['prerelease']:
        raise ValueError('Release is not stable')
    ref = json.loads(fetch(API + '/git/ref/tags/' + version))['object']
    if ref['type'] == 'tag':
        ref = json.loads(fetch(ref['url']))['object']
    if ref['sha'] != commit:
        raise ValueError('Release tag does not resolve to supplied commit')
    destination = ROOT / 'releases' / version
    if destination.exists():
        raise ValueError('Release already exists; review changes instead of overwriting')
    with tempfile.TemporaryDirectory(prefix='ddm-vendor-') as directory:
        temp = Path(directory)
        hashes = {}
        for name in ('docker-compose.yml', 'LICENSE.md'):
            data = fetch(f'https://raw.githubusercontent.com/DefectDojo/django-DefectDojo/{commit}/{name}')
            (temp / name).write_bytes(data)
            hashes[name] = hashlib.sha256(data).hexdigest()
        images = {}
        for name in ('defectdojo-django', 'defectdojo-nginx'):
            token = json.loads(fetch('https://auth.docker.io/token?service=registry.docker.io&scope=repository:defectdojo/' + name + ':pull'))['token']
            data = fetch(f'https://registry-1.docker.io/v2/defectdojo/{name}/manifests/{version}', {
                'Authorization': 'Bearer ' + token,
                'Accept': 'application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json'})
            images[name] = f'docker.io/defectdojo/{name}:{version}@sha256:' + hashlib.sha256(data).hexdigest()
        env = {k: v for k, v in os.environ.items() if not k.startswith(('DD_', 'DJANGO_', 'NGINX_', 'COMPOSE_'))}
        result = subprocess.run(['docker', 'compose', '--env-file', '/dev/null', '-f', str(temp / 'docker-compose.yml'), 'config', '--format', 'json'],
                                env=env, capture_output=True, timeout=60, check=True)
        doc = deployment(json.loads(result.stdout), images)
        if any(not re.search(r'@sha256:[0-9a-f]{64}$', s['image']) for s in doc['services'].values()):
            raise ValueError('Upstream database/broker image lacks a digest; inspect official registry before bundling')
        data = (json.dumps(doc, indent=2) + '\n').encode()
        (temp / 'compose.json').write_bytes(data)
        hashes['compose.json'] = hashlib.sha256(data).hexdigest()
        manifest = {'version': version, 'commit': commit, 'source': f'https://github.com/DefectDojo/django-DefectDojo/tree/{commit}',
                    'release_notes': release['html_url'], 'files': hashes, 'images': {n: s['image'] for n, s in doc['services'].items()}}
        (temp / 'release.json').write_text(json.dumps(manifest, indent=2) + '\n')
        import shutil
        shutil.copytree(temp, destination)
    print(f'Vendored {version} at {commit}; review files and run make validate test before enabling any upgrade path')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('version')
    parser.add_argument('commit')
    args = parser.parse_args()
    vendor(args.version, args.commit)
