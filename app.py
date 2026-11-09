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


class Window(Gtk.Window):
    def __init__(self, recover_current=False):
        super().__init__(title='DVD Auto Rip')
        self.set_default_size(740, 460)
        self.set_border_width(18)
        self.engine = Engine(self.report)
        self.engine.recovery_next_disc = recover_current
        self.config_file = STATE / 'settings.json'
        try:
            config = json.loads(self.config_file.read_text())
        except (OSError, ValueError):
            config = {}
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
        threading.Thread(target=self.engine.watch, daemon=True).start()

    def report(self, message):
        GLib.idle_add(self.show_message, message)

    def show_message(self, message):
        self.status.set_text(message)
        # Percent updates stay in the status label; preserve a readable history.
        if not message.startswith('Encoding:') and message != self.last_message:
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
        self.config_file.write_text(json.dumps({'enabled': self.engine.enabled,
                                               'minimum_minutes': self.engine.minimum_minutes}))

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
    args = parser.parse_args()
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
        config_file = STATE / 'settings.json'
        try:
            config = json.loads(config_file.read_text())
        except (OSError, ValueError):
            config = {}
        engine.enabled = config.get('enabled', True)
        engine.minimum_minutes = config.get('minimum_minutes', 40)
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
    window = Window(recover_current=args.recover_current)
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGUSR1,
                         lambda: (window.show_all(), window.present(), True)[-1])
    window.show_all()
    Gtk.main()
