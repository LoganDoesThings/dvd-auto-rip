#!/usr/bin/python3
"""Install launchers for this checkout, without copying movies or account state."""
import argparse
from pathlib import Path
import shlex


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--no-autostart', action='store_true',
                        help='Do not create a login startup entry (existing entries are preserved).')
    args = parser.parse_args()
    project = Path(__file__).resolve().parent
    home = Path.home()
    launcher = home / '.local/bin/dvd-auto-rip'
    launcher.parent.mkdir(parents=True, exist_ok=True)
    launcher.write_text('#!/bin/sh\nexec /usr/bin/python3 ' + shlex.quote(str(project / 'app.py')) + ' "$@"\n')
    launcher.chmod(0o755)
    # Desktop Exec quoting differs from shell quoting; escape reserved characters.
    executable = str(launcher).replace('\\', '\\\\').replace('"', '\\"').replace('`', '\\`').replace('$', '\\$').replace('%', '%%')
    desktop = ('[Desktop Entry]\nType=Application\nName=DVD Auto Rip\n'
               'Comment=Automatically save movies from inserted DVDs, with audio and subtitles\n'
               f'Exec="{executable}"\nIcon=media-optical-dvd\nTerminal=false\n'
               'Categories=AudioVideo;Video;\nStartupNotify=false\n')
    menu = home / '.local/share/applications/dvd-auto-rip.desktop'
    menu.parent.mkdir(parents=True, exist_ok=True)
    menu.write_text(desktop)
    if not args.no_autostart:
        startup = home / '.config/autostart/dvd-auto-rip.desktop'
        startup.parent.mkdir(parents=True, exist_ok=True)
        startup.write_text(desktop)
    print(f'Installed launcher: {launcher}')
    print(f'Installed menu entry: {menu}')
    print('This checkout must remain in place. No watcher has been started.')


if __name__ == '__main__':
    main()
