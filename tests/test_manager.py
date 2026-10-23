import io
import json
import pathlib
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import dd_manager as dm


ROOT = pathlib.Path(__file__).resolve().parents[1]


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = pathlib.Path(self.tmp.name) / 'state'
        self.m = dm.Manager(ROOT, self.state)
        self.m.init()
        config = dm.read_json(self.state / 'config.json')
        config['backup_dir'] = str(pathlib.Path(self.tmp.name) / 'backups')
        dm.write_json(self.state / 'config.json', config)
        self.calls = []

    def docker(self, args, **kwargs):
        self.calls.append(args)
        text = ' '.join(args)
        if 'pg_dump' in text:
            kwargs['stdout'].write(b'PGDMP-test')
        if '-C /app/media -czf - .' in text:
            with tarfile.open(fileobj=kwargs['stdout'], mode='w:gz') as archive:
                info = tarfile.TarInfo('report.txt')
                info.size = 6
                archive.addfile(info, io.BytesIO(b'report'))
        if 'image inspect' in text:
            return json.dumps([{'Id': 'sha256:' + 'a' * 64, 'RepoDigests': ['repo@sha256:' + 'b' * 64]}])
        return ''

    def prepared(self):
        self.m.load()
        self.m.render()
        self.m.set_phase('ready')

    def test_secrets_and_loopback(self):
        self.m.load()
        self.m.render()
        doc = json.loads((self.state / 'compose.json').read_text())
        self.assertEqual(doc['services']['nginx']['ports'][0]['host_ip'], '127.0.0.1')
        self.assertEqual(len(self.m.secrets['DD_CREDENTIAL_AES_256_KEY']), 32)
        self.assertEqual((self.state / 'secrets.json').stat().st_mode & 0o777, 0o600)
        self.assertNotIn('build', doc['services']['uwsgi'])
        self.assertTrue(all('@sha256:' in s['image'] for s in doc['services'].values()))
        self.assertTrue(any(v['source'] == 'defectdojo_postgres' and v['target'] == '/var/lib/postgresql'
                            for v in doc['services']['postgres']['volumes']))
        self.assertEqual(doc['services']['postgres']['environment']['PGDATA'], '/var/lib/postgresql/18/docker')

    def test_network_is_scoped_to_selected_project(self):
        self.m.load()
        self.m.config['project'] = 'separatepilot'
        self.m.render()
        doc = dm.read_json(self.state / 'compose.json')
        for network in doc.get('networks', {}).values():
            self.assertNotIn('name', network)

    def test_lock_excludes_another_process(self):
        with self.m.lock():
            with self.assertRaises(dm.Failure):
                with dm.Manager(ROOT, self.state).lock():
                    self.fail('lock admitted a second command')

    def test_dry_run_writes_nothing_and_calls_nothing(self):
        with patch('dd_manager.subprocess.Popen') as run:
            fresh = pathlib.Path(self.tmp.name) / 'absent'
            self.assertEqual(dm.main(['--state', str(fresh), '--dry-run', 'install']), 0)
            self.assertFalse(fresh.exists())
            run.assert_not_called()

    def test_failed_pull_precedes_downtime(self):
        self.prepared()
        with patch.object(self.m, 'preflight'), patch.object(self.m, 'pull', side_effect=dm.Failure('pull failed')):
            with self.assertRaises(dm.Failure):
                self.m.install()
        self.assertEqual(self.m.phase()['phase'], 'ready')

    def test_migration_failure_keeps_maintenance(self):
        self.prepared()
        with patch.object(self.m, 'run', side_effect=self.docker), patch.object(self.m, 'migrate', side_effect=dm.Failure('migration failed')):
            with self.assertRaises(dm.Failure):
                self.m.activate()
        self.assertNotEqual(self.m.phase()['phase'], 'ready')
        self.assertFalse(any('nginx' in c and 'up' in c for c in self.calls))

    def test_activation_drains_surviving_writers_before_migration(self):
        self.prepared()
        with patch.object(self.m, 'compose', return_value='worker-id'), patch.object(self.m, 'quiesce', side_effect=dm.Failure('still busy')), patch.object(self.m, 'health'), patch.object(self.m, 'http_check'), patch.object(self.m, 'migrate') as migrate:
            with self.assertRaises(dm.Failure):
                self.m.activate()
            migrate.assert_not_called()

    def test_retry_awaits_running_initializer_without_recreation(self):
        self.prepared()
        image = dm.read_json(self.state / 'compose.json')['services']['initializer']['image']
        def running(args, **kwargs):
            self.calls.append(args)
            if args[-4:] == ['ps', '-q', 'initializer', 'unused']:
                self.fail('invalid ps')
            if args[-3:] == ['ps', '-q', 'initializer']:
                return 'initializer-id\n'
            if args[:2] == ['docker', 'inspect']:
                return image
            if args[:2] == ['docker', 'wait']:
                return '0\n'
            return 'initializer-id\n' if 'initializer' in args and 'ps' in args else ''
        with patch.object(self.m, 'run', side_effect=running):
            self.m.migrate()
        self.assertFalse(any('--force-recreate' in c for c in self.calls))
        self.assertTrue(any(c[:2] == ['docker', 'wait'] for c in self.calls))

    def test_restore_refuses_a_running_initializer(self):
        self.prepared()
        with patch.object(self.m, 'run', side_effect=self.docker), patch.object(self.m, 'quiesce'), patch.object(self.m, 'activate'):
            backup = self.m.backup()
        with patch.object(self.m, 'compose', return_value='initializer-id'), patch.object(self.m, 'validate_dump'), patch.object(self.m, 'stop_service') as stop:
            with self.assertRaises(dm.Failure):
                self.m.restore(backup, confirm='RESTORE defectdojo')
            stop.assert_not_called()

    def test_restore_stop_failure_is_recorded_before_stopping_writers(self):
        self.prepared()
        with patch.object(self.m, 'run', side_effect=self.docker), patch.object(self.m, 'quiesce'), patch.object(self.m, 'activate'):
            backup = self.m.backup()
        with patch.object(self.m, 'run', side_effect=self.docker), patch.object(self.m, 'stop_service', side_effect=dm.Failure('stop timeout')):
            with self.assertRaises(dm.Failure):
                self.m.restore(backup, confirm='RESTORE defectdojo')
        self.assertEqual(self.m.phase()['phase'], 'restoring')

    def test_health_failure_keeps_maintenance(self):
        self.prepared()
        with patch.object(self.m, 'run', side_effect=self.docker), patch.object(self.m, 'migrate'), patch.object(self.m, 'health', side_effect=dm.Failure('unhealthy')), patch('dd_manager.time.monotonic', side_effect=[0, 10000]):
            with self.assertRaises(dm.Failure):
                self.m.activate()
        self.assertNotEqual(self.m.phase()['phase'], 'ready')

    def test_doctor_checks_frontend_and_rejects_missing_worker(self):
        self.prepared()
        def compose(*args, **kwargs):
            if 'valkey-cli' in args:
                return 'PONG\n'
            if args == ('ps', '-q', 'celerybeat'):
                return 'beat-id\n'
            return ''
        with patch.object(self.m, 'compose', side_effect=compose), patch.object(self.m, 'shell', return_value=[{'worker': {'ok': 'pong'}}]), patch.object(self.m, 'http_check') as http:
            self.m.health(frontend=True)
            http.assert_called_once_with(frontend=True)
        with patch.object(self.m, 'compose', side_effect=compose), patch.object(self.m, 'shell', return_value=[]):
            with self.assertRaises(dm.Failure):
                self.m.health()

    def test_backup_failure_never_activates_upgrade(self):
        self.prepared()
        with patch.object(self.m, 'upgrade_edge'), patch.object(self.m, 'preflight'), patch.object(self.m, 'pull'), patch.object(self.m, 'quiesce'), patch.object(self.m, 'backup', side_effect=dm.Failure('dump failed')), patch.object(self.m, 'activate') as activate:
            with self.assertRaises(dm.Failure):
                self.m.upgrade('3.4.201')
            activate.assert_not_called()
        self.assertEqual(self.m.config['version'], '3.4.202')

    def test_unreviewed_upgrade_rejected(self):
        self.m.load()
        with self.assertRaises(dm.Failure):
            self.m.upgrade_edge('3.4.201')

    def test_only_rehearsed_commit_pair_is_allowed(self):
        self.m.load()
        self.m.config['version'] = '3.4.201'
        edge = self.m.upgrade_edge('3.4.202')
        self.assertEqual(edge['to_commit'], dm.read_json(ROOT / 'releases/3.4.202/release.json')['commit'])
        with self.assertRaises(dm.Failure):
            self.m.upgrade_edge('3.4.203')

    def test_backup_roundtrip_metadata_and_corruption(self):
        self.prepared()
        with patch.object(self.m, 'run', side_effect=self.docker), patch.object(self.m, 'quiesce'), patch.object(self.m, 'activate'):
            backup = self.m.backup()
        manifest = dm.verify_backup(backup)
        self.assertEqual(manifest['version'], '3.4.202')
        self.assertIn('images', manifest)
        self.assertEqual(backup.stat().st_mode & 0o777, 0o700)
        (backup / 'database.dump').write_bytes(b'broken')
        with self.assertRaises(dm.Failure):
            dm.verify_backup(backup)

    def test_restore_requires_confirmation_before_docker(self):
        self.prepared()
        with patch.object(self.m, 'run') as run:
            with self.assertRaises(dm.Failure):
                self.m.restore(pathlib.Path('/missing'), confirm=None)
            run.assert_not_called()

    def test_archive_traversal_rejected(self):
        dest = pathlib.Path(self.tmp.name) / 'bad.tar.gz'
        for name in ['../escape', '/absolute']:
            with tarfile.open(dest, 'w:gz') as archive:
                info = tarfile.TarInfo(name)
                archive.addfile(info, io.BytesIO())
            with self.assertRaises(dm.Failure):
                dm.validate_media(dest)

    def test_corrupt_restore_fails_before_any_docker_call(self):
        self.prepared()
        with patch.object(self.m, 'run', side_effect=self.docker), patch.object(self.m, 'quiesce'), patch.object(self.m, 'activate'):
            backup = self.m.backup()
        (backup / 'database.dump').write_bytes(b'corrupt')
        with patch.object(self.m, 'run') as run:
            with self.assertRaises(dm.Failure):
                self.m.restore(backup, confirm='RESTORE defectdojo')
            run.assert_not_called()

    def test_all_dry_run_commands_are_inert(self):
        fresh = pathlib.Path(self.tmp.name) / 'absent'
        commands = [['init'], ['start'], ['stop'], ['status'], ['doctor'], ['backup'],
                    ['check-update'], ['upgrade', '3.4.202'], ['restore', '/not-present']]
        with patch('dd_manager.subprocess.Popen') as run:
            for command in commands:
                self.assertEqual(dm.main(['--state', str(fresh), '--dry-run', *command]), 0)
            run.assert_not_called()
        self.assertFalse(fresh.exists())

    def test_unknown_config_rejected(self):
        config = json.loads((self.state / 'config.json').read_text())
        config['hostname_typo'] = 'oops'
        (self.state / 'config.json').write_text(json.dumps(config))
        with self.assertRaises(dm.Failure):
            self.m.load()

    def test_installed_project_cannot_be_renamed_into_empty_volumes(self):
        self.prepared()
        config = dm.read_json(self.state / 'config.json')
        config['project'] = 'accidentalrename'
        dm.write_json(self.state / 'config.json', config)
        with self.assertRaises(dm.Failure):
            self.m.load()

    def test_failed_command_redacts_secret_stderr(self):
        self.m.load()
        secret = self.m.secrets['DD_DATABASE_PASSWORD']
        with self.assertRaises(dm.Failure) as caught:
            self.m.run([sys.executable, '-c', 'import sys; sys.stderr.write(sys.argv[1]); sys.exit(1)', secret])
        self.assertNotIn(secret, str(caught.exception))
        self.assertIn('[REDACTED]', str(caught.exception))

    def test_upgrade_pull_failure_cannot_stop_or_backup(self):
        self.prepared()
        with patch.object(self.m, 'upgrade_edge'), patch.object(self.m, 'preflight'), patch.object(self.m, 'pull', side_effect=dm.Failure('pull failed')), patch.object(self.m, 'quiesce') as stop, patch.object(self.m, 'backup') as backup:
            with self.assertRaises(dm.Failure):
                self.m.upgrade('3.4.201')
            stop.assert_not_called()
            backup.assert_not_called()

    def test_drain_requires_idle_workers_and_broker(self):
        self.prepared()
        def compose(*args, **kwargs):
            return 'worker' if args == ('ps', '-q', 'celeryworker') else ''
        with patch.object(self.m, 'compose', side_effect=compose), patch.object(self.m, 'stop_service') as stop, patch.object(self.m, 'shell', return_value=1), patch('dd_manager.time.monotonic', side_effect=[0, 0, 10000]), patch('dd_manager.time.sleep'):
            with self.assertRaises(dm.Failure):
                self.m.quiesce()
            self.assertFalse(any(c.args[0] == 'celeryworker' for c in stop.call_args_list))
        self.assertEqual(self.m.phase()['phase'], 'maintenance')

    def test_busy_http_import_prevents_stopping_uwsgi(self):
        self.prepared()
        with patch.object(self.m, 'compose', side_effect=['web-id', '1']), patch.object(self.m, 'run') as run, patch('dd_manager.time.monotonic', side_effect=[0, 0, 10000]), patch('dd_manager.time.sleep'):
            with self.assertRaises(dm.Failure):
                self.m.stop_service('uwsgi')
            run.assert_not_called()

    def test_copy_failure_preserves_upgrade_and_no_retention(self):
        self.prepared()
        with patch.object(self.m, 'run', side_effect=self.docker), patch.object(self.m, 'quiesce'), patch.object(self.m, 'copy_backup', side_effect=dm.Failure('copy failed')), patch.object(self.m, 'retain') as retain:
            with self.assertRaises(dm.Failure):
                self.m.backup(quiesced=True, upgrade=True)
            retain.assert_not_called()
        self.assertEqual(len(list(self.m.backup_root().glob('*-upgrade'))), 1)

    def test_retention_keeps_upgrade_and_newest_daily(self):
        self.prepared()
        with patch.object(self.m, 'run', side_effect=self.docker), patch.object(self.m, 'quiesce'), patch.object(self.m, 'activate'):
            old = self.m.backup(quiesced=True)
            upgrade = self.m.backup(quiesced=True, upgrade=True)
            newest = self.m.backup(quiesced=True)
        import os
        for path in (old, upgrade, newest):
            os.utime(path, (1, 1))
        self.m.retain()
        self.assertFalse(old.exists())
        self.assertTrue(upgrade.exists())
        self.assertTrue(newest.exists())

    def test_install_cannot_migrate_running_deployment(self):
        self.prepared()
        with patch.object(self.m, 'preflight'), patch.object(self.m, 'pull'), patch.object(self.m, 'activate') as activate:
            with self.assertRaises(dm.Failure):
                self.m.install()
            activate.assert_not_called()

    def test_malformed_restore_config_fails_before_stop(self):
        self.prepared()
        with patch.object(self.m, 'run', side_effect=self.docker), patch.object(self.m, 'quiesce'), patch.object(self.m, 'activate'):
            backup = self.m.backup()
        config = dm.read_json(backup / 'config.json')
        config['command_timeout'] = -1
        dm.write_json(backup / 'config.json', config)
        manifest = dm.read_json(backup / 'manifest.json')
        manifest['files']['config.json'] = dm.sha256(backup / 'config.json')
        dm.write_json(backup / 'manifest.json', manifest)
        with patch.object(self.m, 'stop_service') as stop, patch.object(self.m, 'run', side_effect=self.docker):
            with self.assertRaises(dm.Failure):
                self.m.restore(backup, confirm='RESTORE defectdojo')
            stop.assert_not_called()

    def test_subprocess_timeout_and_output_do_not_expose_secrets(self):
        self.m.load()
        secret = self.m.secrets['DD_ADMIN_PASSWORD']
        with self.assertRaises(dm.Failure) as caught:
            self.m.run([sys.executable, '-c', 'import sys,time; print(sys.argv[1],flush=True); time.sleep(30)', secret], timeout=0.1)
        self.assertNotIn(secret, str(caught.exception))

    def test_timeout_terminates_client_descendants(self):
        import time
        self.m.load()
        marker = pathlib.Path(self.tmp.name) / 'descendant-survived'
        child = "import sys,time,pathlib; time.sleep(.5); pathlib.Path(sys.argv[1]).write_text('alive')"
        script = f'import subprocess,sys,time; subprocess.Popen([sys.executable,"-c",{child!r},{str(marker)!r}]); time.sleep(3)'
        start = time.monotonic()
        with self.assertRaises(dm.Failure):
            self.m.run([sys.executable, '-c', script], timeout=0.1)
        self.assertLess(time.monotonic() - start, 2)
        time.sleep(.8)
        self.assertFalse(marker.exists(), 'Docker client descendant survived the deadline')


if __name__ == '__main__':
    unittest.main()
