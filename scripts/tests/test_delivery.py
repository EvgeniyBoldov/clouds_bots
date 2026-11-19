import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class DeliveryTests(unittest.TestCase):
    def test_preview_and_invalid_manifest(self):
        preview = subprocess.run(['bash', 'scripts/release.sh', 'preview', 'release.env'], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(preview.returncode, 0, preview.stderr)
        self.assertIn('Rebuild base:', preview.stdout)
        with tempfile.NamedTemporaryFile(mode='w', delete=False) as manifest:
            manifest.write('RELEASE_VERSION=0.1.0\nRELEASE_VERSION=0.1.1\n')
            name = manifest.name
        try:
            result = subprocess.run(['bash', 'scripts/release.sh', 'preview', name], cwd=ROOT, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
        finally:
            os.unlink(name)

    def test_shell_syntax(self):
        scripts = list((ROOT / 'scripts').glob('*.sh'))
        subprocess.run(['bash', '-n', *map(str, scripts)], check=True)
