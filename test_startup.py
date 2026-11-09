import os
from pathlib import Path
import subprocess
import unittest


class Startup(unittest.TestCase):
    def environment(self):
        return dict(os.environ, DISPLAY='invalid:99', WAYLAND_DISPLAY='invalid', GDK_BACKEND='x11')

    def test_no_display_has_useful_error_without_traceback(self):
        result = subprocess.run(['/usr/bin/python3', 'app.py'], cwd=Path(__file__).parent,
                                env=self.environment(), capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 2)
        self.assertIn('--headless', result.stderr)
        self.assertIn('has not started', result.stderr)
        self.assertNotIn('Traceback', result.stderr)

    def test_headless_start_does_not_require_display(self):
        # Isolate state and suppress physical-disc work while checking the real entry point.
        code = '''import core, tempfile, runpy, sys
from pathlib import Path
with tempfile.TemporaryDirectory() as directory:
    core.STATE = Path(directory)
    core.Engine.watch = lambda self: None
    sys.argv = ['app.py', '--headless']
    runpy.run_path('app.py', run_name='__main__')
'''
        result = subprocess.run(['/usr/bin/python3', '-c', code], cwd=Path(__file__).parent,
                                env=self.environment(), capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('watching mounted DVDs', result.stdout)
        self.assertIn('DVD watcher stopped.', result.stdout)


if __name__ == '__main__':
    unittest.main()
