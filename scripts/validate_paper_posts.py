#!/usr/bin/env python3
"""Check imported drafts against rendered Hugo pages and original report quotes."""

import argparse
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import re
from urllib.parse import unquote, urlparse

import pymupdf

from import_paper_reports import CATALOG, display_source, inside, readings


class Page(HTMLParser):
    def __init__(self, text: str):
        super().__init__(convert_charrefs=True)
        self.ids = set()
        self.links = []
        self.images = []
        self.text = []
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if attrs.get('id'):
            self.ids.add(attrs['id'])
        if tag == 'a' and attrs.get('href'):
            self.links.append(attrs['href'])
        if tag == 'img' and attrs.get('src'):
            self.images.append(attrs['src'])

    def handle_data(self, data):
        self.text.append(data)


def compact(text: str) -> str:
    return re.sub(r'\s+', '', text)


def local_target(build: Path, current: Path, link: str) -> tuple[Path, str] | None:
    url = urlparse(link)
    if url.scheme or url.netloc:
        return None
    path = unquote(url.path)
    target = (build / path.lstrip('/')) if path.startswith('/') else current.parent / path
    if not path:
        target = current
    target = target.resolve()
    if not target.is_relative_to(build.resolve()):
        raise ValueError(f'Link escapes build: {link}')
    if target.is_dir():
        target /= 'index.html'
    return target, unquote(url.fragment)


def validate(site: Path, build: Path, reports: Path, production: Path | None = None) -> dict:
    catalog = json.loads((site / CATALOG).read_text(encoding='utf-8'))
    manifest = json.loads((reports / 'manifest.json').read_text(encoding='utf-8'))
    sources = {e['source']: inside(reports, str(Path(e['report']).with_suffix('.json'))) for e in manifest}
    errors, parsed, quote_count = [], {}, 0
    for relative, expected in catalog['generated_files'].items():
        path = inside(site, relative)
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            errors.append('Generated file missing or modified: ' + relative)
        if relative.endswith('.png') and path.is_file():
            pix = pymupdf.Pixmap(path)
            sample = pix.samples[::37]
            if pix.width < 500 or pix.height < 500 or not sample or min(sample) > 150:
                errors.append('Blank or undersized screenshot: ' + relative)
    entries = catalog['papers']
    entries = entries + [{'slug': 'database-papers-source-reading'}]
    date_path = catalog['date'].replace('-', '/')
    for entry in entries:
        path = build / date_path / entry['slug'] / 'index.html'
        if not path.is_file():
            errors.append('Missing rendered article: ' + entry['slug'])
            continue
        page = Page(path.read_text(encoding='utf-8'))
        parsed[path.resolve()] = page
        if production and (production / date_path / entry['slug'] / 'index.html').exists():
            errors.append('Draft leaked into production build: ' + entry['slug'])
        for link in page.links + page.images:
            try:
                target = local_target(build, path, link)
            except ValueError as error:
                errors.append(str(error))
                continue
            if target:
                file, anchor = target
                if not file.is_file():
                    errors.append(f'Missing local target: {entry["slug"]}: {link}')
                elif anchor and file.suffix == '.html':
                    if file not in parsed:
                        parsed[file] = Page(file.read_text(encoding='utf-8'))
                    if anchor not in parsed[file].ids:
                        errors.append(f'Missing anchor: {entry["slug"]}: {link}')
        if 'source' in entry:
            text = compact(''.join(page.text))
            data = json.loads(sources[entry['source']].read_text(encoding='utf-8'))
            if hashlib.sha256(sources[entry['source']].read_bytes()).hexdigest() != entry['report_sha256']:
                errors.append('Report changed since import: ' + entry['source'])
            for reading in readings(data):
                if compact(display_source(reading['quote'])) not in text:
                    errors.append(f'Quote missing after HTML rendering: {entry["slug"]}: {reading.get("id")}')
                else:
                    quote_count += 1
            for evidence in data['evidence']:
                if compact(display_source(evidence['quote'])) not in text:
                    errors.append(f'Evidence quote missing after rendering: {entry["slug"]}: {evidence["id"]}')
    return {'papers': len(catalog['papers']), 'articles': len(entries),
            'screenshots': sum(len(e['screenshots']) for e in catalog['papers']),
            'close_reading_quotes_preserved_in_html': quote_count,
            'evidence_quotes_checked': sum(e['evidence_count'] for e in catalog['papers']),
            'production_drafts_checked': production is not None,
            'errors': errors, 'passed': not errors}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--site', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--build', type=Path, required=True)
    parser.add_argument('--reports', type=Path, required=True)
    parser.add_argument('--production-build', type=Path)
    args = parser.parse_args()
    result = validate(args.site.resolve(), args.build.resolve(), args.reports.resolve(),
                      args.production_build.resolve() if args.production_build else None)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
