import unittest

from tools.vendor_release import deployment


class VendorTests(unittest.TestCase):
    def test_transform_rejects_unknown_topology(self):
        with self.assertRaises(ValueError):
            deployment({'services': {'mystery': {}}, 'volumes': {}}, {})

    def test_transform_removes_build_source_mounts_and_example_environment(self):
        from dd_manager import SERVICES
        services = {name: {'image': 'pinned@sha256:x', 'environment': {'PASSWORD': 'upstream'},
                    'build': {'context': '/source'}, 'ports': ['8080:8080'],
                    'volumes': [{'type': 'bind', 'source': '/source', 'target': '/app'},
                                {'type': 'volume', 'source': 'media', 'target': '/app/media'}]} for name in SERVICES}
        doc = deployment({'name': 'upstream', 'services': services, 'volumes': {'media': {'name': 'upstream_media'}}},
                         {'defectdojo-django': 'django-pinned', 'defectdojo-nginx': 'nginx-pinned'})
        for service in doc['services'].values():
            self.assertNotIn('build', service)
            self.assertNotIn('ports', service)
            self.assertEqual(service['environment'], {})
            self.assertTrue(all(v['type'] == 'volume' for v in service['volumes']))
        self.assertNotIn('name', doc)
        self.assertEqual(doc['volumes']['media'], {})
