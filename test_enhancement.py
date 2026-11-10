import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from enhancement import Manager, check_destination, make_filter, read_json, run_job, validate_output, write_json


def source_metadata():
    return {'streams': [dict(codec_type='video', codec_name='h264', width=720, height=480,
                            sample_aspect_ratio='32:27', field_order='progressive', color_range='tv', color_space='smpte170m',
                            color_transfer='smpte170m', color_primaries='smpte170m'),
                        dict(codec_type='audio'), dict(codec_type='subtitle')],
            'format': {'duration': '100'}, 'chapters': [{}]}


def enhanced_metadata():
    data = source_metadata()
    data['streams'][0].update(codec_name='hevc', profile='Main 10', width=3840, height=2160,
                              sample_aspect_ratio='1:1', pix_fmt='yuv420p10le',
                              color_transfer='smpte2084', color_primaries='bt2020')
    return data


class Enhancement(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.output = self.root / 'output'
        self.output.mkdir()
        self.source = self.root / 'finished movie.mkv'
        self.source.write_bytes(b'original source bytes')
        self.manager = Manager(self.root / 'state', legacy_root=self.root / 'legacy')

    def test_hdr_processing_performs_conversion_and_keeps_frame_geometry(self):
        filters = make_filter(source_metadata()['streams'][0], '4k_hdr')
        for part in ['transferin=smpte170m', 'transfer=linear', 'geq=', 'transfer=smpte2084',
                     'w=3840:h=2160', 'setsar=1', 'yuv420p10le']:
            self.assertIn(part, filters)

    def test_four_by_three_content_is_pillarboxed_without_stretching(self):
        video=source_metadata()['streams'][0]
        video['sample_aspect_ratio']='8:9'
        filters=make_filter(video,'4k_hdr')
        self.assertIn('w=2880:h=2160',filters)
        self.assertIn('pad=3840:2160:',filters)

    def test_unknown_pixel_aspect_is_not_guessed(self):
        video=source_metadata()['streams'][0]
        video.pop('sample_aspect_ratio')
        with self.assertRaisesRegex(RuntimeError,'pixel aspect ratio'):
            make_filter(video,'4k_hdr')

    def test_sdr_profile_does_not_apply_hdr_upmapping(self):
        filters = make_filter(source_metadata()['streams'][0], '1080p_sdr')
        self.assertIn('w=1920:h=1080', filters)
        self.assertNotIn('geq=', filters)
        self.assertNotIn('smpte2084', filters)

    def test_source_color_information_is_required(self):
        video = source_metadata()['streams'][0]
        video.pop('color_primaries')
        with self.assertRaisesRegex(RuntimeError, 'metadata is missing'):
            make_filter(video, '4k_hdr')

    def test_already_hdr_source_is_not_upmapped_again(self):
        video = source_metadata()['streams'][0]
        video['color_transfer'] = 'smpte2084'
        with self.assertRaisesRegex(RuntimeError, 'SDR sources'):
            make_filter(video, '4k_hdr')

    def test_interlaced_source_requires_separate_treatment(self):
        video = source_metadata()['streams'][0]
        video['field_order'] = 'tt'
        with self.assertRaisesRegex(RuntimeError, 'Deinterlace'):
            make_filter(video, '4k_hdr')

    def test_valid_complete_output_passes(self):
        validate_output(source_metadata(), enhanced_metadata(), '4k_hdr')

    def test_truncation_is_rejected(self):
        output = enhanced_metadata()
        output['format']['duration'] = '50'
        with self.assertRaisesRegex(RuntimeError, 'runtime'):
            validate_output(source_metadata(), output, '4k_hdr')

    def test_lost_subtitles_are_rejected(self):
        output = enhanced_metadata()
        output['streams'].pop()
        with self.assertRaisesRegex(RuntimeError, 'track'):
            validate_output(source_metadata(), output, '4k_hdr')

    def test_lost_chapters_are_rejected(self):
        output = enhanced_metadata()
        output['chapters'] = []
        with self.assertRaisesRegex(RuntimeError, 'chapter'):
            validate_output(source_metadata(), output, '4k_hdr')

    def test_wrong_hdr_signaling_is_rejected(self):
        output = enhanced_metadata()
        output['streams'][0]['color_transfer'] = 'bt709'
        with self.assertRaisesRegex(RuntimeError, 'format/color'):
            validate_output(source_metadata(), output, '4k_hdr')

    def test_duplicate_requests_do_not_launch_a_second_encode(self):
        with patch('enhancement.subprocess.Popen') as start:
            first = self.manager.enqueue(self.source, '4k_hdr', self.output)
            second = self.manager.enqueue(self.source, '4k_hdr', self.output)
        self.assertEqual(first, second)
        self.assertEqual(start.call_count, 1)
        self.assertEqual(self.source.read_bytes(), b'original source bytes')

    def test_existing_standalone_job_is_adopted_without_restart(self):
        folder = self.manager.legacy_root / 'existing job'
        folder.mkdir(parents=True)
        write_json(folder / 'status.json', {'state':'encoding', 'source':str(self.source),
                   'width':3840, 'supervisor_pid':os.getpid(), 'final':str(self.output/'movie.mkv')})
        with patch('enhancement.subprocess.Popen') as start:
            result = self.manager.enqueue(self.source, '4k_hdr', self.output)
        self.assertEqual(result, folder)
        start.assert_not_called()

    def test_failed_job_is_not_automatically_retried_in_a_loop(self):
        with patch('enhancement.subprocess.Popen'):
            folder = self.manager.enqueue(self.source, '4k_hdr', self.output)
        data = read_json(folder/'status.json')
        data['state'] = 'failed'
        write_json(folder/'status.json', data)
        with patch('enhancement.subprocess.Popen') as start:
            self.assertEqual(self.manager.enqueue(self.source, '4k_hdr', self.output), folder)
        start.assert_not_called()

    def test_partial_source_is_not_queued(self):
        source = self.root/'unfinished.partial.mkv'
        source.touch()
        with self.assertRaisesRegex(ValueError, 'finished movie'):
            self.manager.enqueue(source, '4k_hdr', self.output)

    def test_missing_other_drive_is_not_created_on_system_disk(self):
        missing = self.root/'unmounted drive'/'Movies'
        with self.assertRaisesRegex(RuntimeError, 'mount the other drive'):
            self.manager.enqueue(self.source, '4k_hdr', missing)
        self.assertFalse(missing.exists())

    def test_existing_final_is_preserved(self):
        final = self.output/'finished movie - 4K HDR (SDR-converted).mkv'
        final.write_bytes(b'existing finished file')
        with self.assertRaisesRegex(RuntimeError, 'Existing enhanced file'):
            self.manager.enqueue(self.source, '4k_hdr', self.output)
        self.assertEqual(final.read_bytes(), b'existing finished file')

    def prepare_job(self):
        with patch('enhancement.subprocess.Popen'):
            return self.manager.enqueue(self.source, '4k_hdr', self.output)

    def test_low_disk_space_stops_before_encoder_without_touching_original(self):
        folder = self.prepare_job()
        with patch('enhancement.shutil.disk_usage',return_value=SimpleNamespace(free=1)), \
             patch('enhancement.subprocess.Popen') as start:
            run_job(folder)
        self.assertEqual(read_json(folder/'status.json')['state'], 'failed')
        start.assert_not_called()
        self.assertEqual(self.source.read_bytes(), b'original source bytes')

    def test_disconnected_drive_is_detected_before_encoding(self):
        folder = self.prepare_job()
        data=read_json(folder/'status.json')
        data['destination_device'] += 1
        write_json(folder/'status.json',data)
        with patch('enhancement.subprocess.Popen') as start:
            run_job(folder)
        self.assertIn('disconnected', read_json(folder/'status.json')['error'])
        start.assert_not_called()

    def test_queued_stop_never_starts_encoder(self):
        folder = self.prepare_job()
        (folder/'STOP').touch()
        with patch('enhancement.subprocess.Popen') as start:
            run_job(folder)
        self.assertEqual(read_json(folder/'status.json')['state'], 'stopped')
        start.assert_not_called()

    def test_source_replaced_while_queued_is_rejected(self):
        folder = self.prepare_job()
        self.source.write_bytes(b'different source now')
        with patch('enhancement.subprocess.Popen') as start:
            run_job(folder)
        self.assertIn('changed while queued', read_json(folder/'status.json')['error'])
        start.assert_not_called()

    def test_queue_waits_for_worker_lock_and_can_be_cancelled(self):
        import fcntl
        import threading
        folder = self.prepare_job()
        with (self.manager.state/'enhancement-worker.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            timer=threading.Timer(.1,lambda:(folder/'STOP').touch())
            timer.start()
            try:
                with patch('enhancement.subprocess.Popen') as start:
                    run_job(folder)
                start.assert_not_called()
            finally:
                timer.cancel()
        self.assertEqual(read_json(folder/'status.json')['state'],'stopped')

    def test_automatic_enhancement_is_off_by_default(self):
        from app import automatic_enhancement
        engine=SimpleNamespace(on_saved='old',report=lambda message:None)
        with patch('app.Manager') as manager:
            automatic_enhancement(engine,{})
        self.assertIsNone(engine.on_saved)
        manager.assert_not_called()

    def test_opt_in_connects_saved_rip_to_selected_destination_and_profile(self):
        from app import automatic_enhancement
        from unittest.mock import Mock
        engine=SimpleNamespace(on_saved=None,report=lambda message:None)
        manager=Mock()
        manager.enqueue.return_value=self.root/'queued job'
        config={'enhance_after_rip':True,'enhancement_preset':'1080p_hdr',
                'enhancement_destination':str(self.output)}
        automatic_enhancement(engine,config,manager)
        engine.on_saved(self.source)
        manager.enqueue.assert_called_once_with(self.source,'1080p_hdr',str(self.output))

    def test_unmounted_drive_folder_is_not_treated_as_system_disk_output(self):
        with patch('enhancement.os.path.ismount',return_value=False):
            with self.assertRaisesRegex(RuntimeError,'not mounted'):
                check_destination(Path('/media/user/unmounted-drive/Movies'))

    def test_missing_worker_does_not_leave_a_permanently_queued_job(self):
        folder=self.prepare_job()
        os.utime(folder/'status.json',(0,0))
        self.assertEqual(self.manager.jobs()[0]['state'],'interrupted')

    def test_stop_control_works_for_adopted_job(self):
        folder = self.manager.legacy_root / 'existing'
        folder.mkdir(parents=True)
        write_json(folder/'status.json',{'state':'encoding','source':str(self.source),'width':3840,
                   'supervisor_pid':os.getpid(),'final':str(self.output/'movie.mkv')})
        self.assertEqual(self.manager.stop_active(),folder)
        self.assertTrue((folder/'STOP').exists())


if __name__ == '__main__':
    unittest.main()
