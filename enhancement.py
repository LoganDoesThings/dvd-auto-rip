"""Optional, serialized enhancement jobs. Original rips are never overwritten."""
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from fractions import Fraction

HOME = Path.home()
DEFAULT_OUTPUT = HOME / 'Videos/Enhanced Movies'
ACTIVE = {'starting', 'encoding', 'checking_metadata', 'verifying'}
PRESETS = {
    '1080p_sdr': (1920, 1080, False, 6000, '1080p SDR'),
    '1080p_hdr': (1920, 1080, True, 6000, '1080p HDR (SDR-converted)'),
    '4k_hdr': (3840, 2160, True, 8000, '4K HDR (SDR-converted)'),
}
RESERVE = 3 * 1024**3


def now():
    return datetime.now(timezone.utc).isoformat()


def write_json(path, data):
    path = Path(path)
    tmp = path.with_name(path.name + f'.{os.getpid()}.tmp')
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)


def read_json(path):
    return json.loads(Path(path).read_text())


def process_alive(pid):
    try:
        os.kill(int(pid), 0)
        stat = Path(f'/proc/{pid}/stat')
        if stat.exists() and stat.read_text().rsplit(')', 1)[1].split()[0] == 'Z':
            return False
        return True
    except (ProcessLookupError, ValueError, TypeError):
        return False
    except PermissionError:
        return True


def check_destination(destination):
    destination = Path(destination)
    if str(destination).startswith(('/media/', '/run/media/', '/mnt/')):
        if not any(parent != Path('/') and os.path.ismount(parent)
                   for parent in (destination, *destination.parents)):
            raise RuntimeError('Output drive folder is not mounted; original movie preserved.')


def signature(source):
    stat = Path(source).stat()
    return [stat.st_size, stat.st_mtime_ns]


def tools():
    local = HOME / '.local/opt/ffmpeg'
    ff = shutil.which('ffmpeg')
    probe = shutil.which('ffprobe')
    env = dict(os.environ)
    if not ff or not probe:
        ff, probe = str(local / 'usr/bin/ffmpeg'), str(local / 'usr/bin/ffprobe')
        if not Path(ff).is_file() or not Path(probe).is_file():
            raise RuntimeError('Enhancement requires FFmpeg and FFprobe; original rip is preserved.')
        libraries = str(local / 'usr/lib/x86_64-linux-gnu')
        if env.get('LD_LIBRARY_PATH'):
            libraries += os.pathsep + env['LD_LIBRARY_PATH']
        env['LD_LIBRARY_PATH'] = libraries
    return ff, probe, env


def track_counts(data):
    return {kind: sum(s['codec_type'] == kind for s in data['streams'])
            for kind in ('video', 'audio', 'subtitle', 'attachment')}


def make_filter(video, preset):
    width, height, hdr, _, _ = PRESETS[preset]
    if video.get('color_transfer') in ('smpte2084', 'arib-std-b67'):
        raise RuntimeError('These presets accept SDR sources; HDR sources need a separate treatment.')
    if video.get('field_order') not in (None, 'unknown', 'progressive'):
        raise RuntimeError('Deinterlace this source before enhancement.')
    if video['width'] > width or video['height'] > height:
        raise RuntimeError('Selected preset would reduce the source resolution.')
    values = [video.get(k) for k in ('color_space', 'color_transfer', 'color_primaries')]
    if any(not value or value in ('unknown', 'unspecified', 'reserved') for value in values):
        raise RuntimeError('Source color metadata is missing; automatic color conversion was not guessed.')
    matrix, transfer, primaries = values
    if any(not re.fullmatch(r'[a-zA-Z0-9_-]+', value) for value in values):
        raise RuntimeError('Unsupported source color metadata.')
    source_range = {'tv': 'limited', 'pc': 'full'}.get(video.get('color_range'))
    if source_range is None:
        raise RuntimeError('Source color range is missing; original is preserved.')
    try:
        sar = Fraction(video.get('sample_aspect_ratio', '').replace(':', '/'))
        if sar <= 0:
            raise ValueError('invalid SAR')
    except (ValueError, ZeroDivisionError):
        raise RuntimeError('Source pixel aspect ratio is missing; enhancement was not stretched by guessing.')
    display_ratio = Fraction(video['width'], video['height']) * sar
    if display_ratio >= Fraction(width, height):
        scaled_width, scaled_height = width, max(2, int(Fraction(width) / display_ratio) // 2 * 2)
    else:
        scaled_width, scaled_height = max(2, int(height * display_ratio) // 2 * 2), height
    padding = (f',pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black'
               if (scaled_width, scaled_height) != (width, height) else '')
    common = (f'hqdn3d=1:1:2:2,zscale=matrixin={matrix}:transferin={transfer}:'
              f'primariesin={primaries}:rangein={source_range}:matrix=bt709:transfer=bt709:'
              'primaries=bt709:range=limited,format=yuv420p10le,hue=s=1.04,'
              'unsharp=5:5:0.25:5:5:0')
    if not hdr:
        return (common + f',zscale=w={scaled_width}:h={scaled_height}:filter=lanczos:matrix=bt709:'
                'transfer=bt709:primaries=bt709:range=limited:dither=error_diffusion,'
                'format=yuv420p10le,setsar=1' + padding)
    peak = 'max(r(X,Y),max(g(X,Y),b(X,Y)))'
    expressions = ':'.join(f"{channel}='{channel}(X,Y)*(1+2*pow({peak},4))'" for channel in 'rgb')
    return (common + ',zscale=transfer=linear:primaries=bt2020:npl=203,format=gbrpf32le,geq='
            + expressions + f',zscale=w={scaled_width}:h={scaled_height}:filter=lanczos:transfer=smpte2084:'
            'primaries=bt2020:matrix=bt2020nc:range=limited:npl=203:dither=error_diffusion,'
            'format=yuv420p10le,setsar=1' + padding)


def validate_output(source, output, preset):
    width, height, hdr, _, _ = PRESETS[preset]
    video = next((s for s in output['streams'] if s['codec_type'] == 'video'), {})
    expected = (width, height, '1:1', 'yuv420p10le', 'smpte2084' if hdr else 'bt709',
                'bt2020' if hdr else 'bt709')
    actual = tuple(video.get(k) for k in ('width', 'height', 'sample_aspect_ratio', 'pix_fmt',
                                         'color_transfer', 'color_primaries'))
    if actual != expected or video.get('codec_name') != 'hevc' or video.get('profile') != 'Main 10':
        raise RuntimeError('Enhanced output format/color verification failed.')
    if abs(float(output['format']['duration']) - float(source['format']['duration'])) > 1:
        raise RuntimeError('Enhanced output runtime mismatch.')
    if track_counts(source) != track_counts(output):
        raise RuntimeError('Enhanced output lost an original track.')
    if len(source.get('chapters', [])) != len(output.get('chapters', [])):
        raise RuntimeError('Enhanced output lost chapter markers.')


class Manager:
    def __init__(self, state_dir, legacy_root=None):
        self.state = Path(state_dir)
        self.state.mkdir(parents=True, exist_ok=True)
        self.registry = self.state / 'enhancements.json'
        self.legacy_root = Path(legacy_root) if legacy_root is not None else DEFAULT_OUTPUT

    @contextlib.contextmanager
    def registry_lock(self):
        with (self.state / 'enhancements-registry.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def paths(self):
        with self.registry_lock():
            paths = read_json(self.registry) if self.registry.exists() else []
            if not isinstance(paths, list):
                raise RuntimeError('Enhancement registry is unreadable; existing jobs preserved.')
            # Adopt the already-running, pre-integration full-film job without restarting it.
            for candidate in self.legacy_root.glob('*/status.json'):
                if str(candidate.parent) not in paths:
                    try:
                        data = read_json(candidate)
                    except (OSError, ValueError):
                        continue
                    if data.get('source') and data.get('final'):
                        paths.append(str(candidate.parent))
            write_json(self.registry, paths)
            return paths

    def jobs(self):
        jobs = []
        for folder in self.paths():
            try:
                data = read_json(Path(folder) / 'status.json')
            except (OSError, ValueError):
                continue
            data['job_dir'] = folder
            if 'preset' not in data and data.get('width') == 3840:
                data['preset'] = '4k_hdr'
            if data.get('state') in ACTIVE | {'queued'} and data.get('supervisor_pid'):
                if not process_alive(data['supervisor_pid']):
                    data['state'] = 'interrupted'
                    data['error'] = 'Worker is no longer running; partial output preserved.'
            if data.get('state') == 'queued' and not data.get('supervisor_pid'):
                if time.time() - (Path(folder) / 'status.json').stat().st_mtime > 60:
                    data['state'] = 'interrupted'
                    data['error'] = 'Queued worker did not start; retry explicitly.'
            jobs.append(data)
        return jobs

    def enqueue(self, source, preset, destination, retry=False):
        source = Path(source).resolve(strict=True)
        destination = Path(destination).expanduser().resolve()
        if preset not in PRESETS:
            raise ValueError('Unknown enhancement preset.')
        if not source.is_file() or '.partial' in source.name:
            raise ValueError('Select a finished movie, not a partial file.')
        check_destination(destination)
        if destination == DEFAULT_OUTPUT and not destination.exists():
            destination.mkdir(parents=True)
        if not destination.is_dir():
            raise RuntimeError('Choose an existing output folder; mount the other drive first.')
        destination_device = destination.stat().st_dev
        with (self.state / 'enhancement-submit.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            jobs = self.jobs()
            for job in jobs:
                if job.get('source') == str(source) and job.get('preset') == preset:
                    if job.get('state') in ACTIVE | {'queued'}:
                        return Path(job['job_dir'])
                    if job.get('state') == 'complete' and Path(job['final']).is_file():
                        return Path(job['job_dir'])
                    if not retry and job.get('destination', str(Path(job['final']).parent)) == str(destination):
                        return Path(job['job_dir'])
            _, _, _, _, label = PRESETS[preset]
            stem = re.sub(r'[^\w .()-]+', '_', source.stem)[:100]
            final = destination / f'{stem} - {label}.mkv'
            if final.exists():
                raise RuntimeError('Existing enhanced file preserved; choose a different folder.')
            job_id = hashlib.sha256(f'{source}:{preset}:{destination}:{time.time_ns()}'.encode()).hexdigest()[:12]
            folder = destination / f'Enhancement job {job_id}'
            if destination.stat().st_dev != destination_device:
                raise RuntimeError('Output drive changed before job creation.')
            folder.mkdir()
            data = {'state': 'queued', 'source': str(source), 'source_signature': signature(source),
                    'preset': preset, 'destination': str(destination),
                    'destination_device': destination_device,
                    'partial': str(folder / (final.stem + '.partial.mkv')), 'final': str(final),
                    'created_utc': now(), 'state_dir': str(self.state),
                    'legacy_root': str(self.legacy_root), 'percent': 0, 'eta_seconds': None}
            write_json(folder / 'status.json', data)
            with self.registry_lock():
                paths = read_json(self.registry) if self.registry.exists() else []
                paths.append(str(folder))
                write_json(self.registry, paths)
            try:
                with (folder / 'supervisor.log').open('ab') as log:
                    subprocess.Popen([sys.executable, '-u', str(Path(__file__).resolve()), '--worker', str(folder)],
                                     stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            except OSError as exc:
                data.update(state='failed', error=str(exc))
                write_json(folder / 'status.json', data)
                raise
            return folder

    def stop_active(self):
        jobs = self.jobs()
        active = next((j for j in jobs if j.get('state') in ACTIVE), None)
        active = active or next((j for j in jobs if j.get('state') == 'queued'), None)
        if active:
            (Path(active['job_dir']) / 'STOP').touch()
            return Path(active['job_dir'])
        return None


def run_job(folder):
    folder = Path(folder)
    data = read_json(folder / 'status.json')
    manager = Manager(data['state_dir'], data.get('legacy_root'))
    source, destination = Path(data['source']), Path(data['destination'])
    partial, final = Path(data['partial']), Path(data['final'])
    child = None

    def update(**values):
        data.update(values, updated_utc=now(), supervisor_pid=os.getpid())
        write_json(folder / 'status.json', data)

    def check():
        if (folder / 'STOP').exists():
            raise InterruptedError('Enhancement stopped; original and partial are preserved.')
        check_destination(destination)
        if destination.stat().st_dev != data['destination_device']:
            raise RuntimeError('Output drive changed/disconnected; refusing to write to another filesystem.')
        if shutil.disk_usage(destination).free < RESERVE:
            raise RuntimeError('Low disk space; partial preserved and original unchanged.')

    def probe_file(path):
        result = subprocess.run([probe, '-v', 'error', '-show_streams', '-show_format',
                                 '-show_chapters', '-of', 'json', str(path)], env=env,
                                capture_output=True, text=True, check=True, timeout=120)
        if result.stderr.strip():
            raise RuntimeError('Media probe reported errors.')
        return json.loads(result.stdout)

    def stage(args, name):
        nonlocal child
        progress = folder / (name + '-progress.log')
        args = [args[0], '-progress', str(progress), *args[1:]] if name == 'verifying' else args
        with (folder / (name + '.log')).open('wb') as log:
            child = subprocess.Popen(['/usr/bin/nice', '-n', '10', *args], env=env,
                                     stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            update(state=name, child_pid=child.pid, percent=0, eta_seconds=None)
            offset = 0
            pending = ''
            values = {}
            last_progress, best_frame = time.monotonic(), -1
            while child.poll() is None:
                check()
                if progress.exists():
                    with progress.open() as stream:
                        stream.seek(offset)
                        pending += stream.read()
                        offset = stream.tell()
                    lines = pending.split('\n')
                    pending = lines.pop()
                    for line in lines:
                        key, sep, value = line.partition('=')
                        if sep:
                            values[key] = value
                    frame = int(values.get('frame', 0))
                    if frame > best_frame:
                        best_frame, last_progress = frame, time.monotonic()
                    seconds = frame / data['source_fps']
                    try:
                        fps = float(values.get('fps', 0))
                    except ValueError:
                        fps = 0
                    speed = fps / data['source_fps']
                    remaining = max(0, data['source_duration_seconds'] - seconds)
                    update(frames=frame, fps=fps, progress_seconds=seconds,
                           percent=min(100, 100 * seconds / data['source_duration_seconds']),
                           speed=speed, eta_seconds=remaining / speed if speed else None,
                           output_bytes=partial.stat().st_size if partial.exists() else 0,
                           free_disk_bytes=shutil.disk_usage(destination).free)
                if time.monotonic() - last_progress > 900:
                    raise RuntimeError('No encoding/decoding progress for 15 minutes; partial preserved.')
                time.sleep(2)
            result = child.wait()
            child = None
            if result:
                raise RuntimeError(f'{name} failed; see {name}.log.')

    try:
        update(state='queued')
        with (manager.state / 'enhancement-worker.lock').open('a') as lock:
            while True:
                check()
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    time.sleep(2)
            # Earlier standalone jobs do not hold our new queue lock; wait for them too.
            while any(j.get('state') in ACTIVE and j['job_dir'] != str(folder)
                      for j in manager.jobs()):
                check()
                time.sleep(2)
            if signature(source) != data['source_signature']:
                raise RuntimeError('Source changed while queued; original preserved.')
            if partial.exists() or final.exists():
                raise RuntimeError('Existing output preserved; refusing overwrite.')
            ff, probe, env = tools()
            original = probe_file(source)
            write_json(folder / 'source-metadata.json', original)
            video = next(s for s in original['streams'] if s['codec_type'] == 'video')
            numerator, denominator = video['avg_frame_rate'].split('/')
            rate = float(numerator) / float(denominator)
            duration = float(original['format']['duration'])
            if not rate or not duration:
                raise RuntimeError('Source duration/frame rate unavailable.')
            width, height, hdr, maxrate, label = PRESETS[data['preset']]
            required = RESERVE + int(duration * maxrate * 1000 / 8) + source.stat().st_size
            if shutil.disk_usage(destination).free < required:
                raise RuntimeError('Not enough room for the bounded encode plus reserve; choose a larger drive.')
            filters = make_filter(video, data['preset'])
            (folder / 'filter.txt').write_text(filters)
            update(source_duration_seconds=duration, source_fps=rate, width=width, height=height)
            # Multi-threaded HDR filtering produced spatially corrupted but decodable frames
            # on the installed FFmpeg build. Keep filtering serial; x265 stays threaded.
            args = [ff, '-nostdin', '-n', '-hide_banner', '-nostats', '-v', 'warning',
                    '-filter_threads', '1', '-i', str(source), '-map', '0:v:0', '-map', '0:a?',
                    '-map', '0:s?', '-map', '0:t?', '-map_metadata', '0', '-map_chapters', '0',
                    '-vf', filters, '-c:v', 'libx265', '-preset', 'veryfast', '-crf', '18',
                    '-pix_fmt', 'yuv420p10le', '-x265-params',
                    f'pools=4:frame-threads=2:repeat-headers=1:vbv-maxrate={maxrate}:vbv-bufsize={maxrate*2}',
                    '-fps_mode:v', 'passthrough', '-c:a', 'copy', '-c:s', 'copy', '-c:t', 'copy',
                    '-color_primaries', 'bt2020' if hdr else 'bt709',
                    '-color_trc', 'smpte2084' if hdr else 'bt709',
                    '-colorspace', 'bt2020nc' if hdr else 'bt709', '-color_range', 'tv',
                    '-metadata', f'title={source.stem} - {label}',
                    '-progress', str(folder / 'encoding-progress.log'), str(partial)]
            write_json(folder / 'encode-command.json', args)
            stage(args, 'encoding')
            update(state='checking_metadata', eta_seconds=None)
            output = probe_file(partial)
            write_json(folder / 'output-metadata.json', output)
            validate_output(original, output, data['preset'])
            stage([ff, '-nostdin', '-hide_banner', '-nostats', '-v', 'error', '-i', str(partial),
                   '-map', '0:v:0', '-map', '0:a?', '-fps_mode:v', 'vfr', '-f', 'null', '-'], 'verifying')
            if (folder / 'verifying.log').stat().st_size:
                raise RuntimeError('Full output decoding reported errors; partial preserved.')
            check()
            if signature(source) != data['source_signature']:
                raise RuntimeError('Source changed during encoding; original preserved.')
            if final.exists():
                raise RuntimeError('Final output exists; refusing overwrite.')
            partial.rename(final)
            update(state='complete', percent=100, eta_seconds=0, child_pid=None,
                   output_bytes=final.stat().st_size, finished_utc=now())
    except BaseException as exc:
        if child is not None and child.poll() is None:
            os.killpg(child.pid, signal.SIGINT)
            try:
                child.wait(timeout=30)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGTERM)
                child.wait(timeout=10)
        update(state='stopped' if isinstance(exc, (InterruptedError, KeyboardInterrupt)) else 'failed',
               error=str(exc), child_pid=None, eta_seconds=None)
        print(str(exc), file=sys.stderr)


if __name__ == '__main__':
    if len(sys.argv) != 3 or sys.argv[1] != '--worker':
        raise SystemExit('Use DVD Auto Rip to create enhancement jobs.')
    run_job(Path(sys.argv[2]))
