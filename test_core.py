import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from core import Engine, safe_name, seconds, title_set, verify_title


def title(duration=100, audio=1, subtitles=2):
    return {'Duration': {'Hours': 0, 'Minutes': duration // 60, 'Seconds': duration % 60},
            'AudioList': [{}] * audio, 'SubtitleList': [{}] * subtitles, 'VideoCodec': 'h264'}


class Checks(unittest.TestCase):
    def test_parse_json_among_logs(self):
        data = {'TitleList': [title()]}
        self.assertEqual(title_set('diagnostics\nJSON Title Set: ' + json.dumps(data) + '\nmore logs'),
                         [title()])

    def test_missing_scan_does_not_pass(self):
        with self.assertRaises(RuntimeError):
            title_set('HandBrake has exited. Nothing readable.')

    def test_truncated_movie_rejected(self):
        with self.assertRaisesRegex(RuntimeError, 'runtime'):
            verify_title(title(6000), title(15))

    def test_missing_subtitles_rejected(self):
        with self.assertRaisesRegex(RuntimeError, 'subtitle'):
            verify_title(title(), title(subtitles=0))

    def test_missing_audio_rejected(self):
        with self.assertRaisesRegex(RuntimeError, 'audio'):
            verify_title(title(), title(audio=0))

    def test_complete_movie_accepted(self):
        verify_title(title(6000), title(5999))

    def test_label_cannot_escape_output_folder(self):
        self.assertNotIn('/', safe_name('../../Movies; $(touch test)'))

    def engine(self, directory):
        with patch('core.STATE', Path(directory)):
            return Engine(lambda message: None)

    def test_read_error_stops_process(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = self.engine(directory)
            with self.assertRaisesRegex(RuntimeError, 'read/decoder'):
                engine.run_command(['/usr/bin/python3', '-c',
                                    'import time; print("DVDReadBlocks failed", flush=True); time.sleep(60)'],
                                   Path(directory) / 'read.log')

    def test_stderr_cannot_corrupt_stdout_json(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = self.engine(directory)
            # Force a diagnostic to arrive while stdout is halfway through a JSON key.
            code = '''import sys, time
sys.stdout.write('JSON Title Set: {"TitleList": [{"Video')
sys.stdout.flush()
time.sleep(.1)
sys.stderr.write('HandBrake has exited.\\n')
sys.stderr.flush()
time.sleep(.1)
sys.stdout.write('Codec": "mpeg2video"}]}')
sys.stdout.flush()
'''
            log = Path(directory) / 'streams.log'
            result = engine.run_command(['/usr/bin/python3', '-c', code], log)
            self.assertEqual(title_set(result), [{'VideoCodec': 'mpeg2video'}])
            self.assertNotIn('HandBrake has exited.', result)
            self.assertIn('HandBrake has exited.', log.read_text())

    def test_cancel_stops_process(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = self.engine(directory)
            timer = threading.Timer(.2, engine.stop.set)
            timer.start()
            try:
                with self.assertRaisesRegex(RuntimeError, 'Stopped'):
                    engine.run_command(['/usr/bin/python3', '-c', 'import time; time.sleep(60)'],
                                       Path(directory) / 'cancel.log')
            finally:
                timer.cancel()

    def test_repeated_zero_percent_is_not_progress(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = self.engine(directory)
            with self.assertRaisesRegex(RuntimeError, 'No measurable progress'):
                engine.run_command(['/usr/bin/python3', '-c',
                                    'import time\nwhile True: print("Encoding: task 1 of 1, 0.00 %", flush=True); time.sleep(.05)'],
                                   Path(directory) / 'stalled.log', timeout=.2)


if __name__ == '__main__':
    unittest.main()
