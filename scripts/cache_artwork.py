#!/usr/bin/env python3
"""Cache wiki sprite/emblem URLs under assets/, then switch only successful game URLs to local files.

Intended to run in GitHub Actions. It never needs your Google Sheets credentials.
Unresolved pictures remain remote and are listed in assets/download-report.json.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import io
import json
import pathlib
import random
import threading
import time
from urllib.parse import quote, unquote, urlparse

import requests
from PIL import Image, UnidentifiedImageError

ROOT = pathlib.Path(__file__).resolve().parents[1]
MANIFEST = ROOT / 'asset_sources.json'
INDEX = ROOT / 'index.html'
THREAD = threading.local()
UA = 'Mozilla/5.0 (compatible; Ina7PersonalFanSite/1.0; Wikimedia-style image caching)'
HEADERS = {'User-Agent': UA, 'Referer': 'https://inazuma-eleven.fandom.com/', 'Accept': 'image/webp,image/png,image/jpeg,image/*;q=0.8,*/*;q=0.2'}
MAX_IMAGE_BYTES = 5 * 1024 * 1024


def session():
    sess = getattr(THREAD, 'session', None)
    if sess is None:
        sess = requests.Session()
        sess.headers.update(HEADERS)
        THREAD.session = sess
    return sess


def fetch_binary(url):
    r = None
    try:
        r = session().get(url, timeout=(5, 12), stream=True, allow_redirects=True)
        if r.status_code != 200:
            return None, f'HTTP {r.status_code}'
        if 'text/html' in r.headers.get('content-type', '').lower():
            return None, 'HTML response instead of image'
        buffer = bytearray()
        for part in r.iter_content(65536):
            if part:
                buffer.extend(part)
            if len(buffer) > MAX_IMAGE_BYTES:
                return None, 'image over 5 MB'
        with Image.open(io.BytesIO(buffer)) as im:
            im.load()
            if im.width < 8 or im.height < 8:
                return None, 'image unexpectedly small'
            if im.width > 512 or im.height > 512:
                im.thumbnail((256, 256), Image.Resampling.LANCZOS)
            im = im.convert('RGBA')
            dst = io.BytesIO()
            im.save(dst, 'WEBP', lossless=(im.width <= 128 and im.height <= 128), quality=90, method=4)
            return dst.getvalue(), None
    except (requests.RequestException, UnidentifiedImageError, ValueError, OSError) as e:
        return None, str(e)[:140]
    finally:
        if r is not None:
            r.close()


def alternate_url(original):
    p = urlparse(original)
    if p.netloc.lower() == 'inazuma-eleven.fandom.com' and 'Special:FilePath/' in p.path:
        basename = unquote(p.path.rsplit('/', 1)[-1])
        return 'https://inazuma-eleven.fandom.com/wiki/Special:Redirect/file/' + quote(basename)
    return None


def fetch_one(source):
    url = source['url']
    relative = source['path']
    dest = ROOT / relative
    if dest.is_file() and dest.stat().st_size > 60:
        return relative, True, 'already downloaded', url
    candidate_urls = [url]
    alt = alternate_url(url)
    if alt:
        candidate_urls.append(alt)
    last_error = 'not available'
    for attempt_url in candidate_urls:
        for attempt in range(2):
            data, error = fetch_binary(attempt_url)
            if data:
                dest.parent.mkdir(parents=True, exist_ok=True)
                temp = dest.with_suffix('.webp.tmp')
                temp.write_bytes(data)
                temp.replace(dest)
                return relative, True, 'downloaded', url
            last_error = error or last_error
            if error and any(x in error for x in ['HTTP 404','HTTP 403','HTML response','image over']):
                break
            if attempt == 0:
                time.sleep(random.uniform(0.4, 1.4))
    return relative, False, last_error, url


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--dry-run', action='store_true', help='Show planned assets; do not fetch or modify game')
    parser.add_argument('--workers', type=int, default=12)
    args = parser.parse_args()
    manifest = json.loads(MANIFEST.read_text(encoding='utf-8'))
    sources = manifest['sources']
    print(f'Ina7 artwork: {len(sources)} distinct URLs ({sum(v["kind"] == "players" for v in sources)} player variants, {sum(v["kind"] == "badges" for v in sources)} emblem variants)', flush=True)
    if args.dry_run:
        return

    ok, failed, cached = [], [], 0
    workers = max(1, min(20, args.workers))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        tasks = {pool.submit(fetch_one, item): item for item in sources}
        for i, task in enumerate(concurrent.futures.as_completed(tasks), 1):
            rel, success, message, url = task.result()
            if success:
                ok.append((url, rel))
                if message == 'already downloaded':cached += 1
            else:
                failed.append({'url': url, 'asset': rel, 'reason': message})
            if i % 125 == 0 or i == len(sources):
                print(f'[{i}/{len(sources)}] downloaded/cached: {len(ok)}, failed: {len(failed)}', flush=True)

    # Only rewrite URLs for files actually present. Failed requests retain their live URLs.
    text = INDEX.read_text(encoding='utf-8')
    replacements = 0
    for url, rel in ok:
        if url in text:
            text = text.replace(url, './' + rel)
            replacements += 1
    INDEX.write_text(text, encoding='utf-8')

    report = {
        'total_source_urls': len(sources),
        'local_files_present': len(ok),
        'already_cached': cached,
        'unresolved_count': len(failed),
        'game_source_urls_replaced': replacements,
        'unresolved': sorted(failed, key=lambda x: x['asset']),
    }
    (ROOT / 'assets').mkdir(parents=True, exist_ok=True)
    (ROOT / 'assets' / 'download-report.json').write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding='utf-8')
    print(f'Finished: {len(ok)} local images, {len(failed)} unresolved. Game URLs rewritten: {replacements}.', flush=True)
    if failed:
        print('Some sprites could not be obtained. They still use live wiki URLs or in-game fallback initials.', flush=True)


if __name__ == '__main__':
    main()
