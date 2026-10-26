"""Validate release checksums and effective Compose without contacting Docker."""
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dd_manager as dm


def main():
    for release in sorted((dm.ROOT / 'releases').iterdir()):
        if not release.is_dir():
            continue
        for profile in ('config.example.json', 'config.production.example.json'):
            with tempfile.TemporaryDirectory(prefix='ddm-config-') as directory:
                manager = dm.Manager(dm.ROOT, Path(directory) / 'state')
                manager.init()
                config = dm.read_json(manager.state / 'config.json')
                config.update(dm.read_json(dm.ROOT / profile))
                config['version'] = release.name
                dm.write_json(manager.state / 'config.json', config)
                manager.load()
                manager.render()
                manager.compose('config', '--quiet')
                dm.log(f'VALID: {release.name}, {profile}, immutable images, loopback-only published HTTP, no builds/source mounts')


if __name__ == '__main__':
    main()
