# DVD Auto Rip development and recovery notes

## Goal and design

- Watch mounted movie DVDs and save each qualifying long title as a separate MKV.
- Preserve all detected audio and selectable subtitle tracks, DVD resolution, chapter markers and an uncropped picture.
- Keep failed outputs as partial files; never overwrite existing finished movies.
- GTK is the desktop front end; `core.py` owns the watcher, subprocess cancellation, validation and completion history. Headless operation is also supported.

## Lessons from real disc failures

- HandBrake can report exit code 0 while producing a shortened movie. Output duration and track checks are necessary before assigning a finished filename.
- Merging stderr into stdout can insert diagnostics inside HandBrake JSON. Drain both concurrently, save diagnostics, and parse only stdout.
- An inserted DVD may be unmounted. The watcher discovers mounted DVD filesystems; direct reads can also fail independently of mounting or permissions.
- A main-feature DVD-folder copy may omit `VIDEO_TS.IFO`, preventing a whole-folder scan even if VOB files exist. Copied byte count alone is not playback verification.
- Backward timestamps and decoder errors may indicate a DVD-reading/decryption problem. Do not assume scratches or change the drive region based on those warnings alone.
- Fresh title-based reading with key caching disabled corrected a Shrek 2 scan from MPEG-1 to MPEG-2 and produced a clean complete recovery. The exact contribution of authentication method versus cached keys was not isolated.

## Implemented backup plan

- After a primary scan, encode or output validation failure, try the alternate DVD reading method once before reporting a final error.
- Recovery rescans with `DVDCSS_METHOD=title` and `DVDCSS_CACHE=off`. Existing cached keys remain untouched; only the recovery subprocess environment changes.
- Preserve the original duration/track requirements. A rescan cannot quietly reduce them to make the output pass.
- Select every detected subtitle explicitly and disable redundant subtitle searching for recovery; subtitles are retained.
- Use RF18 with x264 veryfast for recovery, versus slow for the primary attempt. This trades larger files for a shorter recovery encode.
- Recovery must pass duration/audio/subtitle checks and a full video/all-audio decoding check. Only then rename its partial output and record completion.
- Stop/Quit does not retry. A failed backup preserves both attempts and ends without looping.
- Stall tracking accounts for separate HandBrake tasks; moving from a completed subtitle scan into encoding is progress.

## Verified result

- Shrek 2 primary: source 1:32:19; output only 1:07:53 with 566 video decoder errors. The duration check correctly rejected it.
- Recovery: 1:32:24, six audio and four subtitle tracks; zero reported video/audio decoder errors and no backward audio/video timestamp jumps. Full output video/all-audio decoding passed.
- Subtitle warnings occurred; metadata verifies presence, not subjective caption timing. Spot-check playback and subtitles.
- Separate additional long titles can represent alternate cuts/content; do not delete or deduplicate them solely by runtime.
- All 26 automated tests passed when the GitHub backup was prepared.

## Useful checks

- `/usr/bin/python3 -m unittest -v`: run regression and startup checks from the project directory.
- `lsblk -o NAME,FSTYPE,LABEL,MOUNTPOINTS`: inspect disc detection and mounting.
- `udisksctl mount -b /dev/sr0`: request a desktop-session mount; authorization or unreadable media may prevent it.
- `~/.local/bin/dvd-auto-rip --recover-current`: use recovery for the next detected disc once, then return to primary-then-backup behavior; quit another watcher first.

## Backup boundaries

- Git tracks source, tests, installer and documentation. Runtime settings, completion history, keys, logs, movie files and disc copies stay on the local machine.
- A clone is not a backup of ripped movies. Install external dependencies separately and use `install.py` to recreate desktop launchers.
- Library settings: https://videolan.videolan.me/libdvdcss/ .
