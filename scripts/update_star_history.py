#!/usr/bin/env python3
"""Record a real GitHub star-count snapshot and render a self-contained SVG."""
import argparse
from datetime import datetime, timezone
from html import escape
import json
import math
from pathlib import Path
import re
import subprocess


def record(data, repo, date, count):
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        raise ValueError('GitHub star count must be a nonnegative integer')
    if data.get('repository', repo) != repo:
        raise ValueError('Existing history belongs to a different repository')
    snapshots = {point['date']: point['stars'] for point in data.get('snapshots', [])}
    snapshots[date] = count
    return {'repository': repo, 'snapshots': [{'date': day, 'stars': snapshots[day]} for day in sorted(snapshots)]}


def render(data):
    points = data['snapshots']
    count = points[-1]['stars']
    maximum = max(1, math.ceil(max(point['stars'] for point in points) / 5) * 5)
    x = lambda index: 556 if len(points) == 1 else 76 + index * 892 / (len(points) - 1)
    y = lambda value: 276 - value * 140 / maximum
    ticks = sorted(set([0, maximum // 2, maximum]))
    grid = ''.join(f'<path d="M76 {y(tick):.1f}H994" stroke="#263a48"/><text x="58" y="{y(tick)+4:.1f}" text-anchor="end" fill="#91a8b7" font-size="12">{tick}</text>' for tick in ticks)
    coords = ' '.join(f'{x(i):.1f},{y(point["stars"]):.1f}' for i, point in enumerate(points))
    graph = f'<polyline points="{coords}" fill="none" stroke="#7ae0c6" stroke-width="3"/>' if len(points) > 1 else ''
    graph += ''.join(f'<circle cx="{x(i):.1f}" cy="{y(point["stars"]):.1f}" r="4" fill="#9af5dd"><title>{point["date"]}: {point["stars"]} stars</title></circle>' for i, point in enumerate(points))
    labels = [(0, points[0]['date'])] if len(points) == 1 else [(0, points[0]['date']), (len(points)-1, points[-1]['date'])]
    dates = ''.join(f'<text x="{x(i):.1f}" y="304" text-anchor="middle" fill="#91a8b7" font-size="12">{date}</text>' for i, date in labels)
    caption = 'History starts with this snapshot.' if len(points) == 1 else 'Real star-count snapshots; counts can decrease when stars are removed.'
    repo = escape(data['repository'])
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="1040" height="370" viewBox="0 0 1040 370" role="img" aria-labelledby="title desc">
<title id="title">{repo} star history</title><desc id="desc">{count} current stars. {caption}</desc>
<rect width="1040" height="370" rx="18" fill="#0d1c27"/>
<g font-family="DejaVu Sans,Arial,sans-serif"><text x="34" y="43" fill="#eaf5fc" font-size="21" font-weight="700">Repository stars</text>
<text x="34" y="72" fill="#9ab0bf" font-size="13">{repo}</text>
<text x="992" y="48" text-anchor="end" fill="#91efd0" font-size="32" font-weight="700">{count}</text>
<text x="992" y="73" text-anchor="end" fill="#9ab0bf" font-size="12">Snapshot · {points[-1]['date']} UTC</text>
{grid}{graph}{dates}<text x="34" y="345" fill="#9ab0bf" font-size="12">{caption} Refresh through Actions → Refresh star history.</text></g></svg>'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', default='LoganDoesThings/dvd-auto-rip')
    args = parser.parse_args()
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', args.repo):
        parser.error('Use OWNER/REPO')
    count = json.loads(subprocess.check_output(['gh', 'api', f'repos/{args.repo}', '--jq', '.stargazers_count'], text=True))
    assets = Path(__file__).resolve().parents[1] / 'docs/assets'
    assets.mkdir(parents=True, exist_ok=True)
    history = assets / 'star-history.json'
    data = json.loads(history.read_text()) if history.exists() else {}
    data = record(data, args.repo, datetime.now(timezone.utc).date().isoformat(), count)
    history.write_text(json.dumps(data, indent=2) + '\n')
    (assets / 'star-history.svg').write_text(render(data) + '\n')
    print(f'{args.repo}: {count} stars; recorded an actual UTC snapshot.')


if __name__ == '__main__':
    main()
