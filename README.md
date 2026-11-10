# DVD Auto Rip

A local Ubuntu desktop app that watches mounted DVD-Video discs and uses installed HandBrakeCLI to save separate movie-length titles automatically.

## Restore or install from GitHub

On Ubuntu 24.04, install the runtime tools and clone the private repository using your GitHub account:

```bash
sudo apt install python3 python3-gi gir1.2-gtk-3.0 handbrake-cli ffmpeg
gh repo clone LoganDoesThings/dvd-auto-rip
cd dvd-auto-rip
/usr/bin/python3 -m unittest -v
/usr/bin/python3 install.py
```

A working DVD-reading/decryption stack must also be installed separately; no external tool binaries or DVD keys are bundled. The installer creates the menu launcher and a login-startup entry pointing to this checkout. Use `install.py --no-autostart` to skip creating a startup entry. It does not launch the app, change existing rip settings/history, or move movies. Keep the checkout at the installed location, or rerun the installer after moving it.

The repository backs up source, tests and documentation. Movies, disc copies, logs, local settings/completion history and keys are excluded. Earlier local `before-recovery` source snapshots remain on disk; Git preserves history for future changes. [Development and recovery notes](docs/development-notes.md) explain the verified backup method and its limits.

## Use

- Open **DVD Auto Rip** in the applications menu, or run `~/.local/bin/dvd-auto-rip` once in your normal desktop terminal.
- If a graphical window cannot open, run `~/.local/bin/dvd-auto-rip --headless` in a normal terminal. This watches DVDs without a window; keep the terminal open and press Ctrl+C to stop. Settings from the GUI are honored. Missing display now produces a clear message rather than a traceback.
- The installer registers it to start at future desktop logins unless `--no-autostart` is used. It must run in the desktop session because the agent terminal cannot access the optical drive.
- Insert a DVD and let Ubuntu mount it. The watcher checks every five seconds. If the desktop does not mount it automatically, open the disc in Files once.
- Movie-length titles (40 minutes or longer by default) each become a separate MKV under `~/Videos/DVD Rips/<disc-label>-<fingerprint>/`.
- English and other available audio/subtitle/caption tracks are included when detected by HandBrake. Original supported audio is passed through; unsupported audio gets lossless FLAC. H.264 RF18 video is re-encoded at DVD resolution; this does not create HD or a lossless video archive.
- No cropping; HQ NTSC/PAL preset chosen from source height. Available subtitles stay selectable rather than burned into the picture. Tracks not present/detected on the disc cannot be created.
- Movie names cannot reliably be read from DVD metadata. Names use the disc label and title index. Discs with alternate cuts or duplicate feature titles may produce extra files. Adjust the minimum for shorter films/episodes.
- Another HandBrakeCLI process causes the watcher to wait. Do not start a separate manual rip after this app starts a job; the external-job check is best effort, not an interprocess lock shared with manual scripts.
- **Stop current rip** requests cancellation and keeps partial files. Wait for the stopped status before removing a DVD. **Retry disc** retries failed titles and skips recorded completed outputs.
- Closing the window hides it but keeps watching. Open its launcher again to show it. **Quit** stops the watcher and its current job.
- Turning off automatic ripping pauses new work; use Stop to cancel a running job.

## Completion and errors

Each job has timestamped logs in its output folder. Interrupted or failed files include `.partial.mkv`; they are never labeled complete. Common DVD read errors stop a job immediately. Repeated status text without percentage/file growth is a stall after ten minutes. There is also a 24-hour job cap.

Before a final filename is assigned, HandBrake scans the output: its runtime must match the source within 10 seconds or 0.2%, whichever is larger, and audio/subtitle counts must be at least those detected in the source. This checks metadata, not every frame. Spot-check playback at the beginning, middle, and end and check captions after completion. At least 12 GB free is required at disc start and 8 GB before each movie. Encodes may still exhaust space for unusually large sources; failed files remain partial.

Completed-title history is `~/.local/share/dvd-auto-rip/completed.json`. Settings are alongside it. Disc fingerprints hash DVD IFO files. Reinserting a completed disc skips existing completed outputs. Removing a completed file permits a new encode on Retry/reinsertion. Existing final files with no matching history are preserved and reported.

## Disable or remove

- Temporarily disable new jobs with the checkbox in the app.
- Disable login startup: remove `~/.config/autostart/dvd-auto-rip.desktop`.
- Quit the app before removing its project, launcher, and applications entry. Saved movies are separate and are not removed.

## Validation

- `cd ~/Projects/dvd-auto-rip && /usr/bin/python3 -m unittest -v`
- Automated tests cover structured title parsing, truncated output, missing tracks, filename containment, read errors, cancellation, stalled progress, missing-display errors, text-only startup and diagnostics arriving midway through JSON output.
- A real 15-second sample is used separately to check the HandBrake encode/scan path.
- Physical recovery verification: Shrek 2 completed in the updated GTK app with matching runtime, all source tracks, and a complete video/all-audio decode check. Subjective playback and caption timing still warrant a spot-check. No network or cloud service is used by this app.

References: [HandBrake subtitles](https://handbrake.fr/docs/en/latest/advanced/subtitles.html), [constant quality](https://handbrake.fr/docs/en/1.8.0/technical/video-cq-vs-abr.html), [desktop autostart](https://specifications.freedesktop.org/autostart-spec/latest/).

### HandBrake JSON stream fix

HandBrake emits structured JSON on stdout and diagnostic messages on stderr. The engine drains both streams concurrently for logs/progress but parses only stdout. This avoids malformed JSON when a diagnostic arrives inside an unfinished JSON write. Restart an already-running watcher after updating the code.

## Automatic backup reader

- A failed initial disc scan, encode, or output runtime/track check triggers **one** backup attempt before the app reports Needs attention. Stop/Quit never triggers a retry. If the backup fails too, the app stops and preserves both attempts and logs; it does not loop.
- The backup rescans the disc with fresh title-based DVD reading (`DVDCSS_METHOD=title`, `DVDCSS_CACHE=off`) and uses that fresh video information. The existing DVD key cache is preserved. A per-title rescan must still satisfy the original runtime and audio/subtitle requirements.
- Backup output retains RF18, DVD resolution, no cropping, all detected audio and selectable subtitles, plus chapter markers. It uses x264 veryfast rather than slow to avoid another lengthy recovery encode; files may be larger. The redundant foreign-audio subtitle search is disabled because all detected subtitle tracks are explicitly included.
- Recovered output must pass the usual runtime/track checks **and** a complete video/all-audio decode using FFmpeg before it receives its final filename and completion-history entry. FFmpeg is found on PATH or under `~/.local/opt/ffmpeg`; missing FFmpeg produces a clear failure and leaves the partial file. A successful decode does not prove that every source frame is free of visible defects.
- Each primary/recovery attempt has its own logs and partial filename. Old partial files are not deleted. At least 8 GB must remain before a per-title backup begins.
- `~/.local/bin/dvd-auto-rip --recover-current` uses the backup reader for the next detected disc once, then returns to ordinary primary-then-backup behavior. Quit an existing watcher before using this startup option. Ordinary Retry disc uses normal automatic fallback rules.
- GUI progress now names the HandBrake task, so subtitle scan and movie encoding are distinguishable. Stall tracking resets correctly when a new task starts.
- Physical verification (November 9): Shrek 2 recovery succeeded in the updated GUI. Source runtime 1:32:19; finished MKV 1:32:24 (within the original tolerance), six audio/four subtitle tracks preserved, zero reported video/audio decoder errors, no backward audio/video timestamp jumps, and a complete error-free video/all-audio decode. Final file: `~/Videos/DVD Rips/SHREK_2_US_16X9-6ea59950/SHREK_2_US_16X9 - Title 10.mkv` (1,960,675,659 bytes). The older 1:07:53 partial remains preserved. Subtitle warnings were logged; caption presence was checked, but subjective subtitle timing was not.
- All 33 automated checks passed. Run checks with `/usr/bin/python3 -m unittest -v` in this project. Recovery regressions include a truncated primary output followed by recovery, double failure, Stop without retry, unchanged track requirements, playback-decoding failure, completion-history skipping, and one-time recovery startup.
- DVD reader environment reference: https://videolan.videolan.me/libdvdcss/ .

Fresh Shrek 2 scanning also reveals title 12 (1:29:47), which the configured all-long-titles policy processes separately. The completed main-title verification above refers to title 10. A saved main movie does not mean every alternate long title on the disc is finished.

## Long scans and ambiguous DVD titles

- Scan progress now counts toward the ten-minute no-progress timer. Increasing title, preview or scan progress keeps a real scan alive; repeated identical status still times out. This corrects the Iron Man 2 scan that was being stopped while progressing.
- Scan progress is shown in the status label, and parsed title metadata is saved as a separate JSON file next to the scan log. Parsing continues to use stdout only.
- If a completed automatic scan finds **20 or more movie-length titles**, the app asks for a title choice rather than automatically encoding every candidate. This threshold flags ambiguity, not proof of decoy titles. It does not skip the initial scan or automatically identify the correct cut.
- Quit the existing watcher and use `~/.local/bin/dvd-auto-rip --recover-current --title NUMBER` to scan/rip only a chosen title on the next detected disc. The title choice is consumed once; subsequent discs return to normal automatic selection. A missing/too-short selected title fails rather than substituting a different movie.
- Iron Man 2 disc `IRON_MAN_2_D1` (fingerprint prefix `b6e31bf3`) advertises 99 titles. A bounded local DVD menu-navigation diagnostic reached title 29 (2:04:30, 18 chapters). This is evidence for this specific disc, not a hardcoded title rule for every edition.
- Physical attempt started November 10: the updated GUI scanned title 29 directly with fresh reading, recognized MPEG-2 with five audio/eleven subtitle tracks, and began encoding. Runtime/track and full-decoding checks still apply; completion has not yet been verified.
