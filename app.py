#!/usr/bin/python3
"""Small GTK desktop front end for the DVD watcher."""
import fcntl
import json
from pathlib import Path
import subprocess
import threading
import argparse
import sys

import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, GLib
from core import Engine, STATE, OUTPUT
from enhancement import Manager, PRESETS, DEFAULT_OUTPUT


def automatic_enhancement(engine, config, manager=None):
    engine.on_saved = None
    if config.get("enhance_after_rip", False):
        manager = manager or Manager(STATE)
        def enqueue(source):
            folder = manager.enqueue(source, config.get("enhancement_preset", "4k_hdr"),
                                     config.get("enhancement_destination", str(DEFAULT_OUTPUT)))
            engine.report(f"Enhancement queued or already recorded: {folder.name}. Original preserved.")
        engine.on_saved = enqueue



class Window(Gtk.Window):
    def __init__(self, recover_current=False, selected_title=None):
        super().__init__(title='DVD Auto Rip')
        self.set_default_size(820, 680)
        self.set_border_width(18)
        self.engine = Engine(self.report)
        self.engine.recovery_next_disc = recover_current
        self.engine.title_next_disc = selected_title
        self.config_file = STATE / 'settings.json'
        try:
            config = json.loads(self.config_file.read_text())
        except (OSError, ValueError):
            config = {}
        self.config = config
        self.enhancements = Manager(STATE)
        self.engine.enabled = config.get('enabled', True)
        self.engine.minimum_minutes = config.get('minimum_minutes', 40)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.add(box)
        label = Gtk.Label(label='Insert a movie DVD. Separate movie-length titles save automatically.')
        label.set_line_wrap(True)
        box.pack_start(label, False, False, 0)
        self.toggle = Gtk.CheckButton(label='Automatically rip inserted DVDs')
        self.toggle.set_active(self.engine.enabled)
        self.toggle.connect('toggled', self.settings)
        box.pack_start(self.toggle, False, False, 0)
        row = Gtk.Box(spacing=10)
        row.pack_start(Gtk.Label(label='Minimum movie length (minutes):'), False, False, 0)
        self.minimum = Gtk.SpinButton.new_with_range(10, 240, 5)
        self.minimum.set_value(self.engine.minimum_minutes)
        self.minimum.connect('value-changed', self.settings)
        row.pack_start(self.minimum, False, False, 0)
        box.pack_start(row, False, False, 0)
        self.enhance_toggle = Gtk.CheckButton(label='Create an enhanced copy after verified rips (optional)')
        self.enhance_toggle.set_active(config.get('enhance_after_rip', False))
        box.pack_start(self.enhance_toggle, False, False, 0)
        enhancement_row = Gtk.Box(spacing=10)
        self.enhance_mode = Gtk.ComboBoxText()
        for key, (_, _, hdr, _, label) in PRESETS.items():
            self.enhance_mode.append(key, label + (' — experimental' if hdr else ''))
        self.enhance_mode.set_active_id(config.get('enhancement_preset', '4k_hdr'))
        enhancement_row.pack_start(self.enhance_mode, False, False, 0)
        self.enhance_destination = Gtk.FileChooserButton(title='Choose enhanced-movie folder',
                                                       action=Gtk.FileChooserAction.SELECT_FOLDER)
        DEFAULT_OUTPUT.mkdir(parents=True, exist_ok=True)
        self.enhancement_destination_path = config.get('enhancement_destination', str(DEFAULT_OUTPUT))
        self.enhance_destination.set_filename(self.enhancement_destination_path)
        enhancement_row.pack_start(self.enhance_destination, True, True, 0)
        box.pack_start(enhancement_row, False, False, 0)
        enhancement_buttons = Gtk.Box(spacing=10)
        for label, callback in [('Enhance a saved movie…', self.enhance_movie),
                                ('Stop enhancement', self.stop_enhancement),
                                ('Open enhanced movies', self.open_enhanced)]:
            button = Gtk.Button(label=label)
            button.connect('clicked', callback)
            enhancement_buttons.pack_start(button, True, True, 0)
        box.pack_start(enhancement_buttons, False, False, 0)
        self.enhance_status = Gtk.Label(label='Enhancement: optional; originals are kept.')
        self.enhance_status.set_line_wrap(True)
        self.enhance_status.set_xalign(0)
        box.pack_start(self.enhance_status, False, False, 0)
        explanation = Gtk.Label(label='HDR is converted from SDR. Long jobs keep running after the ripper closes; use Stop enhancement to cancel.')
        explanation.set_line_wrap(True)
        box.pack_start(explanation, False, False, 0)
        self.enhance_toggle.connect('toggled', self.settings)
        self.enhance_mode.connect('changed', self.settings)
        self.enhance_destination.connect('file-set', self.enhancement_folder_selected)
        self.status = Gtk.Label(label='Watching for a mounted movie DVD…')
        self.status.set_line_wrap(True)
        self.status.set_xalign(0)
        box.pack_start(self.status, False, False, 0)
        self.text = Gtk.TextView(editable=False, cursor_visible=False, wrap_mode=Gtk.WrapMode.WORD_CHAR)
        scroll = Gtk.ScrolledWindow()
        scroll.add(self.text)
        box.pack_start(scroll, True, True, 0)
        buttons = Gtk.Box(spacing=10)
        for caption, callback in [('Open saved movies', self.open_output), ('Retry disc', self.retry),
                                  ('Stop current rip', self.cancel), ('Quit', self.close_app)]:
            button = Gtk.Button(label=caption)
            button.connect('clicked', callback)
            buttons.pack_start(button, True, True, 0)
        box.pack_start(buttons, False, False, 0)
        note = Gtk.Label(label='Closing the window hides it; it keeps watching. Use Quit to stop the app.\n'
                              'Errors preserve partial files and logs. DVD audio/subtitle availability varies.')
        note.set_line_wrap(True)
        box.pack_start(note, False, False, 0)
        self.connect('delete-event', self.hide_window)
        self.last_message = ''
        automatic_enhancement(self.engine, config, self.enhancements)
        GLib.timeout_add(2000, self.refresh_enhancements)
        threading.Thread(target=self.engine.watch, daemon=True).start()

    def report(self, message):
        GLib.idle_add(self.show_message, message)

    def show_message(self, message):
        self.status.set_text(message)
        # Percent updates stay in the status label; preserve a readable history.
        if not message.startswith(('Encoding:', 'Scanning:')) and message != self.last_message:
            buffer = self.text.get_buffer()
            buffer.insert(buffer.get_end_iter(), message + '\n')
            if message.startswith(('Disc finished.', 'Needs attention:')):
                try:
                    subprocess.Popen(['notify-send', 'DVD Auto Rip', message],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                except OSError:
                    pass
        self.last_message = message
        return False

    def settings(self, *_):
        if not hasattr(self, 'minimum'):
            return
        self.engine.enabled = self.toggle.get_active()
        self.engine.minimum_minutes = self.minimum.get_value_as_int()
        self.config.update(enabled=self.engine.enabled, minimum_minutes=self.engine.minimum_minutes)
        if hasattr(self, 'enhance_destination'):
            self.config.update(enhance_after_rip=self.enhance_toggle.get_active(),
                               enhancement_preset=self.enhance_mode.get_active_id() or '4k_hdr',
                               enhancement_destination=self.enhancement_destination_path)
            automatic_enhancement(self.engine, dict(self.config), self.enhancements)
        self.config_file.write_text(json.dumps(self.config, indent=2))

    def enhancement_folder_selected(self, *_):
        selected = self.enhance_destination.get_filename()
        if selected:
            self.enhancement_destination_path = selected
            self.settings()

    def enhance_movie(self, *_):
        dialog = Gtk.FileChooserDialog(title='Choose a finished movie to enhance', parent=self,
                                       action=Gtk.FileChooserAction.OPEN)
        dialog.add_buttons('Cancel', Gtk.ResponseType.CANCEL, 'Enhance', Gtk.ResponseType.OK)
        video_filter = Gtk.FileFilter()
        video_filter.set_name('Movie files')
        for pattern in ('*.mkv', '*.mp4', '*.mov'):
            video_filter.add_pattern(pattern)
        dialog.add_filter(video_filter)
        dialog.set_current_folder(str(OUTPUT))
        response = dialog.run()
        filename = dialog.get_filename()
        dialog.destroy()
        if response == Gtk.ResponseType.OK and filename:
            try:
                folder = self.enhancements.enqueue(filename, self.enhance_mode.get_active_id() or '4k_hdr',
                                                   self.enhancement_destination_path, retry=True)
                self.enhance_status.set_text(f'Enhancement queued or already running: {folder.name}. Original preserved.')
            except Exception as exc:
                self.enhance_status.set_text(f'Enhancement needs attention: {exc}')

    def stop_enhancement(self, *_):
        try:
            stopped = self.enhancements.stop_active()
            self.enhance_status.set_text('Stopping enhancement; partial will be kept.' if stopped else 'No enhancement is running.')
        except Exception as exc:
            self.enhance_status.set_text(f'Enhancement needs attention: {exc}')

    def open_enhanced(self, *_):
        folder = self.enhancement_destination_path
        if not Path(folder).is_dir():
            self.enhance_status.set_text('Output folder unavailable; mount the selected drive first.')
            return
        subprocess.Popen(['xdg-open', folder])

    def refresh_enhancements(self):
        try:
            jobs = self.enhancements.jobs()
            active = next((j for j in jobs if j.get('state') in ('encoding', 'verifying', 'checking_metadata', 'starting')), None)
            active = active or next((j for j in jobs if j.get('state') == 'queued'), None)
            job = active or (jobs[-1] if jobs else None)
            if job:
                state = job.get('state', 'unknown')
                eta = job.get('eta_seconds')
                remaining = f', about {eta / 3600:.1f} hours left' if eta is not None and state in ('encoding', 'verifying') else ''
                title = Path(job['source']).stem
                detail = f" {float(job.get('percent', 0)):.1f}%{remaining}" if active else ''
                error = f" — {job['error']}" if job.get('error') else ''
                self.enhance_status.set_text(f'Enhancement: {title} — {state}{detail}{error}')
        except Exception as exc:
            self.enhance_status.set_text(f'Enhancement status unavailable: {exc}')
        return True

    def retry(self, *_):
        if self.engine.busy:
            self.report('A job is running. Stop it and wait before retrying.')
        else:
            self.engine.seen.clear()
            self.report('Retry requested. Completed outputs will be skipped.')

    def cancel(self, *_):
        self.engine.stop.set()
        self.report('Stopping current job; please wait before removing the DVD.')

    def open_output(self, *_):
        OUTPUT.mkdir(parents=True, exist_ok=True)
        subprocess.Popen(['xdg-open', str(OUTPUT)])

    def hide_window(self, *_):
        self.hide()
        return True

    def close_app(self, *_):
        self.engine.quit.set()
        self.engine.stop.set()
        self.status.set_text('Stopping the watcher and any active rip…')
        GLib.timeout_add(250, self.finish_quit)

    def finish_quit(self):
        if self.engine.busy:
            return True
        Gtk.main_quit()
        return False


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Automatically save movies from mounted DVDs.')
    parser.add_argument('--headless', action='store_true',
                        help='Watch DVDs in this terminal without opening a desktop window.')
    parser.add_argument('--recover-current', action='store_true',
                        help='Use the backup DVD reader for the next detected disc, then return to normal.')
    parser.add_argument('--title', type=int, metavar='NUMBER',
                        help='Scan and rip only this title on the next detected disc, then return to automatic selection.')
    parser.add_argument('--enhance', type=Path, metavar='MOVIE', help='Queue an enhanced copy of a saved movie.')
    parser.add_argument('--enhancement-preset', choices=tuple(PRESETS), default='4k_hdr')
    parser.add_argument('--enhancement-output', type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument('--enhancement-status', action='store_true', help='Print enhancement job status without starting the watcher.')
    args = parser.parse_args()
    if args.enhance or args.enhancement_status:
        manager = Manager(STATE)
        if args.enhance:
            print(manager.enqueue(args.enhance, args.enhancement_preset, args.enhancement_output, retry=True))
        else:
            print(json.dumps(manager.jobs(), indent=2))
        raise SystemExit(0)
    if args.title is not None and args.title < 1:
        parser.error('--title must be a positive integer')
    if not args.headless and not Gtk.init_check([])[0]:
        print('DVD Auto Rip cannot connect to a graphical desktop.\n'
              'Open DVD Auto Rip from the Ubuntu applications menu, or run\n'
              '~/.local/bin/dvd-auto-rip --headless in your regular terminal\n'
              'for text-only operation. The DVD watcher has not started.', file=sys.stderr)
        raise SystemExit(2)
    STATE.mkdir(parents=True, exist_ok=True)
    lock = (STATE / 'app.lock').open('w')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        # Bring a hidden watcher forward when its launcher is opened again.
        import os
        import signal
        pid = STATE / 'app.pid'
        try:
            os.kill(int(pid.read_text()), signal.SIGUSR1)
        except (OSError, ValueError):
            pass
        print('DVD Auto Rip is already running. If it is in text mode, use its original terminal.')
        raise SystemExit(0)
    import os
    import signal
    (STATE / 'app.pid').write_text(str(os.getpid()))
    if args.headless:
        def report(message):
            print(message, flush=True)

        engine = Engine(report)
        engine.recovery_next_disc = args.recover_current
        engine.title_next_disc = args.title
        config_file = STATE / 'settings.json'
        try:
            config = json.loads(config_file.read_text())
        except (OSError, ValueError):
            config = {}
        engine.enabled = config.get('enabled', True)
        engine.minimum_minutes = config.get('minimum_minutes', 40)
        automatic_enhancement(engine, config)
        # Ignore GUI reopen requests while in terminal mode.
        signal.signal(signal.SIGUSR1, lambda *_: None)
        def stop_watcher(*_):
            engine.quit.set()
            engine.stop.set()
        signal.signal(signal.SIGINT, stop_watcher)
        signal.signal(signal.SIGTERM, stop_watcher)
        report('DVD Auto Rip: watching mounted DVDs. Keep this terminal open; Ctrl+C stops it.')
        if not engine.enabled:
            report('Automatic ripping is disabled in settings. Enable it in the desktop app.')
        engine.watch()
        report('DVD watcher stopped.')
        raise SystemExit(0)
    window = Window(recover_current=args.recover_current, selected_title=args.title)
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGUSR1,
                         lambda: (window.show_all(), window.present(), True)[-1])
    window.show_all()
    Gtk.main()
