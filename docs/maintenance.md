# Repository maintenance

## Layout

| Path | Purpose |
| --- | --- |
| `app.py` | GTK window and command-line entry point |
| `core.py` | DVD watching, ripping, recovery and verification |
| `enhancement.py` | Optional enhancement queue and workers |
| `install.py` | User launchers and optional login startup |
| `test_*.py` | Regression and startup tests |
| `docs/usage.md` | Full operating reference |
| `docs/development-notes.md` | Design and recovery lessons |
| `docs/assets/` | Self-contained banner and star-history chart/data |
| `scripts/update_star_history.py` | Authenticated GitHub star-count snapshots |
| `.github/workflows/` | Regression checks and manual chart refresh |

## Checks

Run `/usr/bin/python3 -m unittest -v` after code changes. The Tests workflow does the same on Ubuntu 24.04 with GTK bindings installed. It has read-only repository permissions and does not access local movies, account files or drives. The 63-test count describes the suite when this page was prepared; CI is the live pass/fail indicator.

## Refresh the star chart

The bundled SVG is based on actual UTC daily snapshots of GitHub’s `stargazers_count`. Multiple refreshes on the same day replace that day’s count; earlier daily snapshots remain. Stars can go down as well as up. With a single initial snapshot, the chart says so instead of drawing made-up history.

Use **Actions → Refresh star history → Run workflow** to refresh and commit the chart. This workflow is manual, not a scheduled background writer. It uses GitHub’s built-in token with contents-write permission; no personal token is committed. Pull its chart commit before your next local push.

Or, from an authenticated local checkout:

```bash
python3 scripts/update_star_history.py --repo LoganDoesThings/dvd-auto-rip
git add docs/assets/star-history.json docs/assets/star-history.svg
git commit -m 'Refresh repository star snapshot'
git push
```

Chart data is public repository metadata only. The update script can authenticate for private repositories too; no external chart service receives a token. Visibility is never changed by the script/workflow.

## Keep private files out

Only source, tests, installer and documentation belong in Git. Keep movie/disc files, raw runtime logs, keys, account state, job queues and local source snapshots outside version control. Review `git diff --cached` before publishing. No license or release policy is implied by the page redesign.
