"""DVD watcher engine. No GUI imports; subprocesses use argv, never a shell."""
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import signal
import subprocess
import threading
import time

HOME = Path.home()
STATE = HOME / '.local/share/dvd-auto-rip'
OUTPUT = HOME / 'Videos/DVD Rips'
RECOVERY_ENV = {'DVDCSS_METHOD': 'title', 'DVDCSS_CACHE': 'off'}


class CancelledError(RuntimeError):
    """A user stop must never start a recovery attempt."""


BAD_READ = re.compile(r'DVDReadBlocks failed|vobu read error|Error reading from DVD|No initial PTS found', re.I)


def title_set(text):
    marker = 'JSON Title Set: '
    if marker not in text:
        raise RuntimeError('HandBrake did not return readable title information. See the log.')
    return json.JSONDecoder().raw_decode(text.split(marker, 1)[1].lstrip())[0]['TitleList']


def seconds(title):
    d = title['Duration']
    return d['Hours'] * 3600 + d['Minutes'] * 60 + d['Seconds']


def safe_name(value):
    return re.sub(r'[^\w .()-]+', '_', value).strip(' .')[:90] or 'DVD'


def find_discs():
    """Use actual mounted filesystems, not arbitrary folders named VIDEO_TS."""
    discs = []
    for line in Path('/proc/self/mountinfo').read_text().splitlines():
        fields = line.split()
        mount = Path(re.sub(r'\\([0-7]{3})', lambda m: chr(int(m[1], 8)), fields[4]))
        after = fields[fields.index('-') + 1:]
        if not (str(mount).startswith('/media/') or str(mount).startswith('/run/media/')):
            continue
        video = mount / 'VIDEO_TS'
        try:
            if not (video / 'VIDEO_TS.IFO').is_file():
                continue
            digest = hashlib.sha256()
            for item in sorted(video.glob('*.IFO')):
                digest.update(item.name.encode())
                digest.update(item.read_bytes())
            discs.append((digest.hexdigest(), mount, after[1]))
        except OSError:
            continue
    return discs


class Engine:
    def __init__(self, report, output=OUTPUT):
        self.report = report
        self.output = Path(output)
        self.enabled = True
        self.minimum_minutes = 40
        self.stop = threading.Event()
        self.quit = threading.Event()
        self.seen = set()
        self.busy = False
        self.recovery_next_disc = False
        self.title_next_disc = None
        self.on_saved = None
        STATE.mkdir(parents=True, exist_ok=True)
        self.history_file = STATE / 'completed.json'
        try:
            self.history = json.loads(self.history_file.read_text())
        except (OSError, ValueError):
            self.history = {}

    def run_command(self, argv, log, timeout=600, watch_file=None,
                    env_overrides=None, stderr_is_error=False):
        """Cancel process groups; require progress rather than repeated status text."""
        log.parent.mkdir(parents=True, exist_ok=True)
        transcript = bytearray()
        diagnostics = bytearray()
        best = (-1, -1.0)
        decode_time = -1
        scan_best = (-1, -1, -1.0)
        scan_diagnostics = bytearray()
        stderr_seen = False
        last_size = 0
        last_progress = time.monotonic()
        started = last_progress
        env = dict(os.environ, LC_ALL='C')
        env.update(env_overrides or {})
        with log.open('wb') as stream:
            proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    start_new_session=True, env=env)
            selector = selectors.DefaultSelector()
            selector.register(proc.stdout, selectors.EVENT_READ)
            selector.register(proc.stderr, selectors.EVENT_READ)
            try:
                while selector.get_map():
                    if self.stop.is_set() or self.quit.is_set():
                        raise CancelledError('Stopped. Partial output and logs were preserved.')
                    now = time.monotonic()
                    if now - last_progress > timeout or now - started > 24 * 3600:
                        raise RuntimeError('No measurable progress for 10 minutes, or job exceeded 24 hours. See logs.')
                    for key, _ in selector.select(timeout=1):
                        chunk = os.read(key.fd, 16384)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        stream.write(chunk)
                        stream.flush()
                        # HandBrake writes JSON to stdout and diagnostics to stderr.
                        # Merging them can insert a diagnostic inside a JSON string.
                        if key.fileobj is proc.stdout:
                            transcript.extend(chunk)
                            scan_diagnostics.extend(chunk)
                            del scan_diagnostics[:-32768]
                            scan_text = scan_diagnostics.decode(errors='replace')
                            states = re.findall(r'"Scanning":\s*\{([^{}]*)\}', scan_text)
                            for state in states:
                                try:
                                    scanning = json.loads('{' + state + '}')
                                    scan_progress = (int(scanning.get('Title', 0)),
                                                     int(scanning.get('Preview', 0)),
                                                     float(scanning.get('Progress', 0)))
                                except (ValueError, TypeError):
                                    continue
                                if scan_progress > scan_best:
                                    scan_best = scan_progress
                                    last_progress = now
                                    self.report(f"Scanning: title {scanning.get('Title', 0)}/"
                                                f"{scanning.get('TitleCount', '?')}, "
                                                f"{float(scanning.get('Progress', 0)):.0%}")
                        elif chunk.strip():
                            stderr_seen = True
                        diagnostics.extend(chunk)
                        del diagnostics[:-32768]
                        text = diagnostics.decode(errors='replace')
                        if BAD_READ.search(text):
                            raise RuntimeError('DVD read/decoder error. Partial output preserved; see logs.')
                        matches = re.findall(r'Encoding: task (\d+) of \d+, ([0-9.]+) %', text)
                        if matches:
                            task, percent = matches[-1]
                            progress = (int(task), float(percent))
                            self.report(f'Encoding: task {task}, {percent}%')
                            if progress > best:
                                best = progress
                                last_progress = now
                        times = re.findall(r'out_time_us=(\d+)', text)
                        if times and int(times[-1]) > decode_time:
                            decode_time = int(times[-1])
                            last_progress = now
                    if watch_file and watch_file.exists():
                        size = watch_file.stat().st_size
                        if size > last_size:
                            last_size = size
                            last_progress = now
                if proc.wait(timeout=10) != 0:
                    raise RuntimeError(f'Command failed. See {log.name}')
                if stderr_is_error and stderr_seen:
                    raise RuntimeError(f'Playback decoding errors. See {log.name}')
                return transcript.decode(errors='replace')
            finally:
                selector.close()
                if proc.poll() is None:
                    os.killpg(proc.pid, signal.SIGTERM)
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        os.killpg(proc.pid, signal.SIGKILL)
                        proc.wait()
                proc.stdout.close()
                proc.stderr.close()

    def scan(self, device, index, log, recovery=False):
        raw = self.run_command(['HandBrakeCLI', '-i', str(device), '-t', str(index),
                                '--scan', '--json', '--min-duration', '600', '--previews', '1'],
                               log, env_overrides=RECOVERY_ENV if recovery else None)
        try:
            titles = title_set(raw)
            log.with_suffix('.json').write_text(json.dumps(titles, indent=2))
            return titles
        except (ValueError, KeyError, TypeError) as exc:
            raise RuntimeError(f'Unreadable title information. See {log.name}') from exc

    def check_cancelled(self):
        if self.stop.is_set() or self.quit.is_set():
            raise CancelledError('Stopped. Partial output and logs were preserved.')

    def recovery_decode(self, partial, log):
        local = HOME / '.local/opt/ffmpeg'
        binary = shutil.which('ffmpeg')
        env = None
        if not binary and (local / 'usr/bin/ffmpeg').is_file():
            binary = str(local / 'usr/bin/ffmpeg')
            libraries = str(local / 'usr/lib/x86_64-linux-gnu')
            if os.environ.get('LD_LIBRARY_PATH'):
                libraries += os.pathsep + os.environ['LD_LIBRARY_PATH']
            env = {'LD_LIBRARY_PATH': libraries}
        if not binary:
            raise RuntimeError('Recovery needs FFmpeg for its full playback check; partial preserved.')
        self.report('Recovery: checking the entire movie video and all audio tracks.')
        self.run_command([binary, '-nostdin', '-hide_banner', '-v', 'error',
                          '-i', str(partial), '-map', '0:v:0', '-map', '0:a',
                          '-fps_mode:v', 'vfr', '-progress', 'pipe:1', '-f', 'null', '-'],
                         log, env_overrides=env, stderr_is_error=True)

    def attempt_title(self, title, expected, device, final, logs, recovery=False):
        self.check_cancelled()
        index = title['Index']
        mode = 'recovery' if recovery else 'primary'
        partial = final.with_name(final.stem + f'.{time.time_ns()}.{mode}.partial.mkv')
        preset = 'HQ 576p25 Surround' if title['Geometry']['Height'] > 480 else 'HQ 480p30 Surround'
        env = RECOVERY_ENV if recovery else None
        preset_args = ['-Z', preset]
        if recovery:
            # All source subtitle tracks are selected explicitly below: no search pass is needed.
            exported = self.run_command(['HandBrakeCLI', '-Z', preset,
                                          '--preset-export', 'DVD Auto Rip recovery'],
                                         logs / f'title-{index}-recovery-preset.log', env_overrides=env)
            data = json.JSONDecoder().raw_decode(exported[exported.index('{'):])[0]
            settings = data['PresetList'][0]
            settings.update(SubtitleAddForeignAudioSearch=False,
                            SubtitleAddForeignAudioSubtitle=False,
                            SubtitleTrackSelectionBehavior='none', SubtitleList=[])
            preset_file = logs / f'title-{index}-recovery-preset.json'
            preset_file.write_text(json.dumps(data, indent=2))
            preset_args = ['--preset-import-file', str(preset_file), '-Z', 'DVD Auto Rip recovery']
        command = ['HandBrakeCLI', '-i', str(device), '-t', str(index), *preset_args,
                   '-f', 'av_mkv', '-e', 'x264', '--encoder-preset',
                   'veryfast' if recovery else 'slow', '-q', '18', '--crop-mode', 'none',
                   '-a', ','.join(str(i + 1) for i in range(len(title.get('AudioList', [])))) or 'none',
                   '-E', 'copy', '--audio-copy-mask', 'aac,ac3,eac3,truehd,dts,dtshd,mp2,mp3,flac,opus',
                   '--audio-fallback', 'flac24',
                   '-s', ','.join(str(i + 1) for i in range(len(title.get('SubtitleList', [])))) or 'none',
                   '--subtitle-burned=none', '--markers', '-o', str(partial)]
        self.run_command(command, logs / f'title-{index}-{mode}-encode.log',
                         watch_file=partial, env_overrides=env)
        if not partial.exists() or partial.stat().st_size < 1024:
            raise RuntimeError('No usable output file produced.')
        self.report(f'Checking runtime, audio and subtitle tracks for title {index}.')
        raw = self.run_command(['HandBrakeCLI', '-i', str(partial), '--scan', '--json', '--previews', '1'],
                               logs / f'title-{index}-{mode}-verify.log')
        checked = title_set(raw)
        verify_title(expected, checked[0] if checked else {})
        if recovery:
            self.recovery_decode(partial, logs / f'title-{index}-recovery-decode.log')
        self.check_cancelled()
        return partial

    def queue_enhancement(self, final):
        if self.on_saved is not None:
            try:
                self.on_saved(final)
            except Exception as exc:
                # An enhancement failure never invalidates an already verified original rip.
                self.report(f'Enhancement needs attention; original movie saved: {exc}')

    def process(self, identity, mount, device, recovery_only=False, title_index=None):
        self.stop.clear()
        self.check_cancelled()
        if title_index is not None and (not isinstance(title_index, int) or title_index < 1):
            raise ValueError('Selected title must be a positive integer.')
        if not Path(device).is_file() and not Path(device).is_block_device():
            raise RuntimeError(f'DVD device {device} is unavailable. Launch from your desktop session.')
        self.output.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(self.output).free < 12 * 1024**3:
            raise RuntimeError('At least 12 GB free is required before starting a disc.')
        label = safe_name(mount.name)
        folder = self.output / f'{label}-{identity[:8]}'
        folder.mkdir(exist_ok=True)
        logs = folder / 'Logs' / time.strftime('%Y%m%d-%H%M%S')
        logs.mkdir(parents=True, exist_ok=True)
        self.report(f'Scanning {label}. This can take several minutes.')
        recovering = recovery_only
        try:
            scanned = self.scan(device, title_index or 0, logs / 'scan.log', recovery=recovering)
        except CancelledError:
            raise
        except RuntimeError as exc:
            self.check_cancelled()
            if recovering:
                raise
            self.report(f'Initial scan failed: {exc}. Trying the backup DVD reader once.')
            recovering = True
            scanned = self.scan(device, title_index or 0, logs / 'recovery-scan.log', recovery=True)
        titles = [t for t in scanned if seconds(t) >= self.minimum_minutes * 60
                  and (title_index is None or t['Index'] == title_index)]
        if title_index is None and len(titles) >= 20:
            raise RuntimeError(f'This disc has {len(titles)} movie-length titles. Select the movie title '
                               'before retrying; refusing to rip dozens of possibly duplicate versions. '
                               'Quit and restart with --title NUMBER, or request help identifying the menu-selected title.')
        if not titles:
            raise RuntimeError(f'No titles of at least {self.minimum_minutes} minutes found. Adjust the minimum and retry.')
        self.report(f'Found {len(titles)} movie-length title(s). Saving each separately.')
        for number, title in enumerate(titles, 1):
            self.check_cancelled()
            index = title['Index']
            final = folder / f'{label} - Title {index:02d}.mkv'
            key = f'{identity}:{index}'
            if self.history.get(key) == str(final) and final.exists():
                self.report(f'Title {index} already completed; skipping.')
                self.queue_enhancement(final)
                continue
            if final.exists():
                raise RuntimeError(f'Existing untracked output preserved: {final}')
            if shutil.disk_usage(folder).free < 8 * 1024**3:
                raise RuntimeError('Less than 8 GB free. Stopping before the next title.')
            self.report(f'Movie {number}/{len(titles)}: title {index}, {seconds(title)//60} minutes.')
            try:
                partial = self.attempt_title(title, title, device, final, logs, recovery=recovering)
            except CancelledError:
                raise
            except RuntimeError as primary_error:
                self.check_cancelled()
                if recovering:
                    raise
                if shutil.disk_usage(folder).free < 8 * 1024**3:
                    raise RuntimeError('Primary attempt failed; less than 8 GB remains for recovery.') from primary_error
                (logs / f'title-{index}-primary-failure.txt').write_text(str(primary_error))
                self.report(f'Title {index} did not pass: {primary_error}. Trying the backup DVD reader once.')
                try:
                    refreshed = self.scan(device, index, logs / f'title-{index}-recovery-scan.log', recovery=True)
                    fresh = next((t for t in refreshed if t['Index'] == index), None)
                    # A fresh scan must not silently weaken the original runtime/track requirements.
                    verify_title(title, fresh or {})
                    partial = self.attempt_title(fresh, title, device, final, logs, recovery=True)
                except CancelledError:
                    raise
                except RuntimeError as recovery_error:
                    raise RuntimeError(f'Primary attempt failed ({primary_error}); backup attempt also failed '
                                       f'({recovery_error}). Partial files and logs preserved.') from recovery_error
            self.check_cancelled()
            if final.exists():
                raise RuntimeError(f'Existing output preserved: {final}')
            partial.rename(final)
            self.history[key] = str(final)
            tmp = self.history_file.with_suffix('.tmp')
            tmp.write_text(json.dumps(self.history, indent=2))
            tmp.replace(self.history_file)
            self.report(f'Saved: {final.name}')
            self.queue_enhancement(final)
        self.report(f'Disc finished. Files: {folder}. Spot-check playback and captions before ejecting.')

    def watch(self):
        while not self.quit.is_set():
            try:
                discs = find_discs()
                present = {d[0] for d in discs}
                self.seen.intersection_update(present)
                if self.enabled:
                    for identity, mount, device in discs:
                        if identity in self.seen:
                            continue
                        # In the desktop session this sees other manually started jobs too.
                        if subprocess.run(['pgrep', '-x', 'HandBrakeCLI'], stdout=subprocess.DEVNULL).returncode == 0:
                            self.report('Waiting: another HandBrake job is running.')
                            break
                        self.seen.add(identity)
                        self.busy = True
                        try:
                            recovery_only = self.recovery_next_disc
                            self.recovery_next_disc = False
                            selected_title = self.title_next_disc
                            self.title_next_disc = None
                            options = {'recovery_only': recovery_only}
                            if selected_title is not None:
                                options['title_index'] = selected_title
                            self.process(identity, mount, device, **options)
                        except Exception as exc:
                            self.report(f'Needs attention: {exc}')
                        finally:
                            self.busy = False
            except Exception as exc:
                self.report(f'Watcher: {exc}')
            self.quit.wait(5)


def verify_title(source, result):
    if not result or abs(seconds(source) - seconds(result)) > max(10, seconds(source) * .002):
        raise RuntimeError('Output runtime does not match the source. Partial output preserved.')
    for key, name in [('AudioList', 'audio'), ('SubtitleList', 'subtitle/caption')]:
        if len(result.get(key, [])) < len(source.get(key, [])):
            raise RuntimeError(f'Output is missing expected {name} tracks. Partial output preserved.')
    if not result.get('VideoCodec'):
        raise RuntimeError('Output video stream was not detected.')
