import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from core import CancelledError, Engine, RECOVERY_ENV


def movie(duration=6000, audio=6, subtitles=4):
    return {'Index': 10, 'Duration': {'Hours': duration // 3600,
            'Minutes': duration % 3600 // 60, 'Seconds': duration % 60},
            'Geometry': {'Height': 480}, 'VideoCodec': 'mpeg2video',
            'AudioList': [{} for _ in range(audio)],
            'SubtitleList': [{} for _ in range(subtitles)]}


class Recovery(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.device = self.root / 'dvd'
        self.device.touch()
        state = patch('core.STATE', self.root / 'state')
        disk = patch('core.shutil.disk_usage', return_value=SimpleNamespace(free=100 * 1024**3))
        state.start()
        disk.start()
        self.addCleanup(state.stop)
        self.addCleanup(disk.stop)
        self.messages = []
        self.engine = Engine(self.messages.append, self.root / 'output')
        self.calls = []
        self.encodes = []
        self.outputs = {}
        self.primary_result = movie(100)
        self.recovery_result = movie()
        self.fresh = movie()
        self.cancel_primary = False
        self.bad_decode = False
        self.fail_scan = False
        self.scan_failed = False

    def command(self, args, log, **kwargs):
        args = list(map(str, args))
        self.calls.append((args, kwargs))
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text('test log')
        if '--preset-export' in args:
            return json.dumps({'PresetList': [{}]})
        if '--scan' in args:
            source = args[args.index('-i') + 1]
            if source == str(self.device):
                if self.fail_scan and not kwargs.get('env_overrides') and not self.scan_failed:
                    self.scan_failed = True
                    raise RuntimeError('scan failed')
                titles = [self.fresh if kwargs.get('env_overrides') else movie()]
            else:
                titles = [self.outputs[source]]
            return 'JSON Title Set: ' + json.dumps({'TitleList': titles})
        if '-o' in args:
            recovering = kwargs.get('env_overrides') == RECOVERY_ENV
            self.encodes.append(recovering)
            output = Path(args[args.index('-o') + 1])
            output.write_bytes(b'x' * 2048)
            self.outputs[str(output)] = self.recovery_result if recovering else self.primary_result
            if self.cancel_primary and not recovering:
                raise CancelledError('Stopped')
            return ''
        if self.bad_decode:
            raise RuntimeError('Playback decoding errors')
        self.assertTrue(kwargs.get('stderr_is_error'))
        return ''

    def run_disc(self, **kwargs):
        with patch.object(self.engine, 'run_command', side_effect=self.command), \
             patch('core.shutil.which', return_value='/fake/ffmpeg'):
            self.engine.process('fingerprint', Path('SHREK_2'), self.device, **kwargs)

    def test_runtime_failure_recovers_once_and_only_then_records_completion(self):
        self.run_disc()
        self.assertEqual(self.encodes, [False, True])
        folder = self.root / 'output/SHREK_2-fingerpr'
        self.assertTrue((folder / 'SHREK_2 - Title 10.mkv').exists())
        self.assertEqual(len(list(folder.glob('*.primary.partial.mkv'))), 1)
        self.assertEqual(self.engine.history['fingerprint:10'], str(folder / 'SHREK_2 - Title 10.mkv'))
        commands = [args for args, kw in self.calls if '-o' in args]
        self.assertIn('1,2,3,4,5,6', commands[-1])
        self.assertIn('1,2,3,4', commands[-1])
        preset = next(folder.rglob('*recovery-preset.json'))
        self.assertFalse(json.loads(preset.read_text())['PresetList'][0]['SubtitleAddForeignAudioSearch'])
        self.assertTrue(any(kw.get('stderr_is_error') for args, kw in self.calls))

    def test_second_failure_preserves_both_partials_without_completion(self):
        self.recovery_result = movie(100)
        with self.assertRaisesRegex(RuntimeError, 'backup attempt also failed'):
            self.run_disc()
        self.assertEqual(self.encodes, [False, True])
        self.assertFalse(self.engine.history)
        self.assertEqual(len(list(self.engine.output.rglob('*.partial.mkv'))), 2)
        self.assertFalse(list(self.engine.output.rglob('SHREK_2 - Title 10.mkv')))

    def test_user_stop_never_starts_recovery(self):
        self.cancel_primary = True
        with self.assertRaises(CancelledError):
            self.run_disc()
        self.assertEqual(self.encodes, [False])
        self.assertFalse(self.engine.history)

    def test_successful_primary_never_retries(self):
        self.primary_result = movie()
        self.run_disc()
        self.assertEqual(self.encodes, [False])

    def test_recovery_cannot_weaken_source_runtime_or_track_requirements(self):
        self.fresh = movie(subtitles=0)
        with self.assertRaisesRegex(RuntimeError, 'subtitle'):
            self.run_disc()
        self.assertEqual(self.encodes, [False])
        self.assertFalse(self.engine.history)

    def test_decode_failure_never_assigns_final_name(self):
        self.bad_decode = True
        with self.assertRaisesRegex(RuntimeError, 'Playback decoding'):
            self.run_disc()
        self.assertFalse(self.engine.history)
        self.assertFalse(list(self.engine.output.rglob('SHREK_2 - Title 10.mkv')))

    def test_scan_failure_uses_backup_without_repeating_primary_encode(self):
        self.fail_scan = True
        self.run_disc()
        self.assertEqual(self.encodes, [True])

    def test_recovery_only_does_not_repeat_known_failed_primary(self):
        self.run_disc(recovery_only=True)
        self.assertEqual(self.encodes, [True])

    def test_repeated_disc_skips_completed_title(self):
        self.run_disc()
        self.encodes.clear()
        self.run_disc()
        self.assertFalse(self.encodes)

    def test_malformed_scan_is_reported_as_recoverable_failure(self):
        with patch.object(self.engine, 'run_command', return_value='JSON Title Set: {broken'):
            with self.assertRaisesRegex(RuntimeError, 'Unreadable title information'):
                self.engine.scan(self.device, 0, self.root / 'broken.log')

    def test_watcher_consumes_recovery_request_once(self):
        self.engine.recovery_next_disc = True
        calls = []
        def process(*args, **kwargs):
            calls.append(kwargs)
            self.engine.quit.set()
        with patch('core.find_discs', return_value=[('fingerprint', Path('SHREK_2'), self.device)]), \
             patch('core.subprocess.run', return_value=SimpleNamespace(returncode=1)), \
             patch.object(self.engine, 'process', side_effect=process):
            self.engine.watch()
        self.assertEqual(calls, [{'recovery_only': True}])
        self.assertFalse(self.engine.recovery_next_disc)

    def test_subprocess_stderr_errors_reject_exit_zero(self):
        log = self.root / 'decode.log'
        with self.assertRaisesRegex(RuntimeError, 'Playback decoding errors'):
            self.engine.run_command(['/usr/bin/python3', '-c',
                                     'import sys; sys.stderr.write("corrupt decoded frame")'],
                                    log, stderr_is_error=True)

    def test_subprocess_environment_is_scoped_to_recovery(self):
        result = self.engine.run_command(['/usr/bin/python3', '-c',
                                         'import os; print(os.environ["DVDCSS_CACHE"])'],
                                        self.root / 'env.log', env_overrides=RECOVERY_ENV)
        self.assertEqual(result.strip(), 'off')


if __name__ == '__main__':
    unittest.main()
