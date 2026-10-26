"""Opt-in real Docker test; creates and removes only its own random project."""
import http.cookiejar
import os
from pathlib import Path
import secrets
import shutil
import socket
import tempfile
import unittest
import urllib.parse
import urllib.request

import dd_manager as dm


@unittest.skipUnless(os.environ.get('DDM_INTEGRATION') == '1', 'set DDM_INTEGRATION=1 to run isolated Docker lifecycle')
class DockerLifecycle(unittest.TestCase):
    def test_install_backup_restore_upgrade(self):
        with tempfile.TemporaryDirectory(prefix='ddm-integration-') as work:
            root = Path(work)
            shutil.copytree(dm.ROOT / 'releases', root / 'releases')
            shutil.copyfile(dm.ROOT / 'config.example.json', root / 'config.example.json')
            m = dm.Manager(root, root / 'state')
            m.init()
            config = dm.read_json(m.state / 'config.json')
            project = 'ddmtest' + secrets.token_hex(5)
            with socket.socket() as listener:
                listener.bind(('127.0.0.1', 0))
                port = listener.getsockname()[1]
            config.update(project=project, port=port, site_url=f'http://localhost:{port}', version='3.4.201')
            # Tiny fixture data; production headroom must be measured separately.
            config.update(disk_reserve_gib=1, backup_estimate_gib=1, restore_estimate_gib=1)
            config['image_storage_dir'] = os.environ.get('DDM_IMAGE_STORAGE_DIR')
            dm.write_json(m.state / 'config.json', config)
            m.load()
            with m.lock():
                try:
                    m.install()
                    cookies = http.cookiejar.CookieJar()
                    client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookies))
                    with client.open(f'http://localhost:{port}/login', timeout=15) as response:
                        self.assertEqual(response.status, 200)
                    csrf = next(cookie.value for cookie in cookies if cookie.name == 'csrftoken')
                    form = urllib.parse.urlencode({'username': 'admin', 'password': m.secrets['DD_ADMIN_PASSWORD'], 'csrfmiddlewaretoken': csrf}).encode()
                    with client.open(f'http://localhost:{port}/login', data=form, timeout=30) as response:
                        self.assertNotIn('/login', response.url)
                    self.assertTrue(any(cookie.name == 'sessionid' for cookie in cookies))
                    m.shell("from dojo.models import Product_Type; import json; Product_Type.objects.create(name='ddm-backup-sentinel'); print('DD_RESULT=0')")
                    m.compose('exec', '-T', 'uwsgi', 'python', '-c', "from pathlib import Path; Path('/app/media/ddm-sentinel.txt').write_text('before-backup')")
                    original_secrets = dict(m.secrets)
                    backup = m.backup()
                    m.shell("from dojo.models import Product_Type; Product_Type.objects.filter(name='ddm-backup-sentinel').delete(); print('DD_RESULT=0')")
                    m.compose('exec', '-T', 'uwsgi', 'python', '-c', "from pathlib import Path; Path('/app/media/ddm-sentinel.txt').write_text('after-backup')")
                    m.restore(backup, confirm=f'RESTORE {project}')
                    self.assertEqual(m.secrets, original_secrets)
                    self.assertEqual(m.shell("from dojo.models import Product_Type; import json; print('DD_RESULT=' + json.dumps(Product_Type.objects.filter(name='ddm-backup-sentinel').exists()))"), True)
                    content = m.compose('exec', '-T', 'uwsgi', 'cat', '/app/media/ddm-sentinel.txt')
                    self.assertEqual(content, 'before-backup')
                    old, _ = dm.bundle(root / 'releases/3.4.201')
                    new, _ = dm.bundle(root / 'releases/3.4.202')
                    # This policy exists only in the isolated test root during rehearsal.
                    dm.write_json(root / 'releases/upgrade-paths.json', {'paths': [{
                        'from': '3.4.201', 'to': '3.4.202', 'from_commit': old['commit'],
                        'to_commit': new['commit'], 'tested': True,
                        'notes': ['https://docs.defectdojo.com/releases/os_upgrading/3.4.200/'],
                        'evidence': 'isolated rehearsal in progress'}]})
                    m.upgrade('3.4.202')
                    self.assertEqual(m.phase()['phase'], 'ready')
                    self.assertTrue(m.shell("from dojo.models import Product_Type; import json; print('DD_RESULT=' + json.dumps(Product_Type.objects.filter(name='ddm-backup-sentinel').exists()))"))
                    self.assertEqual(m.compose('exec', '-T', 'uwsgi', 'cat', '/app/media/ddm-sentinel.txt'), 'before-backup')
                    dm.log('INTEGRATION PASS: install 3.4.201 -> backup -> mutate -> restore -> upgrade 3.4.202; database/media/secrets verified')
                finally:
                    if (m.state / 'compose.json').exists():
                        m.compose('down', '--volumes', '--remove-orphans', '--timeout', '30')
                    volumes = m.run(['docker', 'volume', 'ls', '--filter', f'label=com.docker.compose.project={project}', '--format', '{{.Name}}']).split()
                    if volumes:
                        m.run(['docker', 'volume', 'rm', *volumes])
