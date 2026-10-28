#!/usr/bin/env python3
"""Import evidence-grounded reports into Hugo leaf bundles, without API calls."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import date
import hashlib
import html
import json
from pathlib import Path
import re
import unicodedata
from urllib.parse import quote, unquote

import pymupdf


VERSION = 1
CATALOG = Path('data/paper-reading-import.json')
INDEX_SLUG = 'database-papers-source-reading'
TOPICS = {
    'optimizer-framework': ('优化器框架', 'Query Optimizer'),
    'join-order': ('连接重排', 'Join Reordering'),
    'cost-model': ('代价模型', 'Cost Model'),
    'statistics': ('统计与基数估计', 'Cardinality Estimation'),
    'functional-dependencies': ('属性与函数依赖', 'Physical Properties'),
    'nested-query': ('嵌套查询与去相关', 'Query Unnesting'),
    'execution-engine': ('查询执行引擎', 'Query Execution'),
    'architecture': ('数据库架构', 'Database Architecture'),
    'consensus': ('共识协议', 'Consensus'),
    'consistency': ('分布式一致性', 'Consistency'),
    'diagnosis-and-tuning': ('诊断与自动调优', 'Database Tuning'),
    'essentials': ('关系模型与查询语言', 'Relational Model'),
    'formats': ('分析型数据格式', 'Columnar Formats'),
    'network': ('网络与系统协同', 'RDMA'),
    'probabilistic-counting': ('概率计数与抽样', 'Sketches'),
    'rdbms': ('数据库系统实践', 'Database Systems'),
    'storage-media': ('存储介质与缓存', 'Storage Hierarchy'),
    'storage-structure': ('存储结构与索引', 'Storage Engine'),
    'transaction': ('事务与并发控制', 'Concurrency Control'),
}
SECTION_ORDER = ('problem', 'architecture', 'algorithm', 'correctness',
                 'experiments', 'tradeoffs', 'comparison', 'reproduction', 'conclusion')
BASIS = {'paper': '论文陈述', 'inference': '读者推断', 'proposal': '待验证的工程设想',
         'mixed': '论文陈述与读者分析，需结合段落中的限定阅读'}


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def inside(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError(f'Path escapes its root: {relative}')
    return path


def clean(text: str) -> str:
    # Some model sections encode paragraph breaks twice. Do not apply to quotes/code.
    return str(text).replace('\\n', '\n').strip()


def label(text: str) -> str:
    return re.sub(r'([\\`*{}\[\]<>#|])', r'\\\1', str(text)).replace('\n', ' ')


def display_source(text: str) -> str:
    """Make PDF font-control artifacts explicit rather than silently lose glyphs."""
    return re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]',
                  lambda match: f'[U+{ord(match.group()):04X}]', str(text))


def blockquote(text: str, *, literal: bool = False) -> str:
    if literal:
        # Raw code text bypasses Goldmark passthrough and MathJax. Its appearance
        # is prose-like via scoped CSS, but the quoted symbols remain verbatim.
        encoded = html.escape(display_source(text).strip(), quote=False).replace('\n', '&#10;')
        # A physical blank line terminates a Goldmark raw HTML block. Numeric
        # line breaks preserve the source while keeping it one HTML block.
        return ('<blockquote class="paper-source-quote"><pre><code>'
                + encoded + '</code></pre></blockquote>')
    text = re.sub(r'([πσ])\[([^\]]+)\]', r'\1\\[\2\\]', str(text))
    return '\n'.join('> ' + line for line in str(text).strip().splitlines())


def normalized_name(text: str) -> str:
    return ''.join(c.lower() for c in unicodedata.normalize('NFKD', text) if c.isalnum())


def source_links(index: Path | None, url: str | None) -> dict[str, str]:
    if not index or not url:
        return {}
    result = {}
    for line in index.read_text(encoding='utf-8').splitlines():
        match = re.search(r'^- \[(.*)\]\((.*\.pdf)\)', line)
        if match:
            paper_title, target = match.groups()
            relative = unquote(target).removeprefix('./')
            if relative.startswith('papers/'):
                result[normalized_name(Path(relative).name)] = url.rstrip('/') + '/' + quote(relative[7:], safe='/')
            elif target.startswith('https://'):
                result[normalized_name(paper_title + '.pdf')] = target
    return result


def readings(data: dict) -> list[dict]:
    close = data.get('close_readings', [])
    if isinstance(close, list):
        return [dict(x, id=x.get('id', f'P{i:03}')) for i, x in enumerate(close, 1)]
    by_id = {x['passage_id']: x for x in close.get('readings', [])}
    result = []
    for passage in data.get('original_passages', []):
        reading = by_id[passage['id']]
        result.append({**reading, **passage})
    return result


def topic(source: str) -> tuple[str, str]:
    if any(x in Path(source).name for x in ('neurocard', 'deep-unsupervised', 'learning-optimizer')):
        return TOPICS['statistics']
    return TOPICS.get(source.split('/')[0], ('数据库系统', 'Database Systems'))


def title(data: dict) -> str:
    value = str(data['report']['title'])
    for word in ('深度研究报告', '深入研究报告', '论文研究报告', '原文复核报告', '研究报告'):
        value = value.replace(word, '')
    return value.strip(' ：:—-')


def bibliography(text: str) -> str:
    paragraph = clean(text).split('\n\n')[0]
    markers = ('方法：', '方法与来源：', '方法声明：', 'API读取了', 'API全文读取',
               'Codex另', 'Codex 复核', '此次为Codex', '使用SHA匹配', '正文来自逐页文本提取')
    cuts = [paragraph.index(marker) for marker in markers if marker in paragraph]
    return paragraph[:min(cuts)].strip() if cuts else paragraph


def slug_for(source: str) -> str:
    stem = unicodedata.normalize('NFKD', Path(source).stem).encode('ascii', 'ignore').decode()
    stem = re.sub(r'[^a-z0-9]+', '-', stem.lower()).strip('-')[:86].rstrip('-')
    return 'paper-' + stem


def screenshot_pages(doc: pymupdf.Document, sections: list[dict]) -> list[dict]:
    """Choose two source pages; captions are a locator heuristic, not visual verification."""
    candidates = []
    for index, page in enumerate(doc):
        text = page.get_text()
        captions = [line.strip() for line in text.splitlines()
                    if re.match(r'^(?:Figure|Fig\.|Table)\s*[\dIVX]+', line.strip(), re.I)]
        candidates.append({'page': index + 1, 'captions': captions[:5],
                           'graphics': len(page.get_images()) + len(page.get_drawings())})
    selected = []
    for role, ids in [('mechanism', {'architecture', 'algorithm', 'correctness'}),
                      ('evaluation', {'experiments', 'tradeoffs'})]:
        cited = {int(p) for s in sections if s.get('id') in ids for p in s.get('pages', [])}
        available = [c for c in candidates if c['page'] not in {s['page'] for s in selected}]
        if not available:
            break
        def score(c: dict) -> tuple:
            return (bool(c['captions']), c['page'] in cited,
                    min(c['graphics'], 40), -c['page'])
        # If there are no recognizable captions, prefer cited graphic pages; otherwise
        # retain one cited text page rather than fabricate a diagram.
        with_captions = [c for c in available if c['captions'] and c['page'] in cited]
        pool = with_captions or [c for c in available if c['page'] in cited] or available
        best = max(pool, key=score)
        if not best['captions'] and not best['graphics'] and selected:
            continue
        selected.append({**best, 'role': role, 'file': f"source-page-{best['page']:03}.png"})
    return selected


def frontmatter(meta: dict) -> str:
    lines = ['---']
    for key, value in meta.items():
        lines.append(f'{key}: {json.dumps(value, ensure_ascii=False)}')
    return '\n'.join(lines + ['---', ''])


def relref(bundle: str) -> str:
    return '{{< relref "' + bundle + '/index.md" >}}'


def render_article(entry: dict, data: dict, previous: dict | None, following: dict | None,
                   index_bundle: str, date_string: str) -> str:
    report = data['report']
    summary = clean(report['executive_summary'])
    description = re.sub(r'[*`#\[\]]', '', summary).replace('\n', ' ')
    if len(description) > 180:
        description = description[:177] + '…'
    text = frontmatter({'title': '【Paper】' + entry['title'],
                        'date': date_string + 'T12:00:00+08:00',
                        'lastmod': date_string + 'T12:00:00+08:00',
                        'slug': entry['slug'], 'categories': ['数据库'],
                        'tags': ['论文精读', entry['topic'], entry['topic_en']],
                        'description': description, 'draft': True, 'toc': True,
                        'math': True, 'paper_reading': True})
    nav = [f"本组第 {entry['number']} 篇 · {entry['topic']}", f'[全部论文]({relref(index_bundle)})']
    if previous:
        nav.append(f"[上一篇]({relref(previous['bundle'])})")
    if following:
        nav.append(f"[下一篇]({relref(following['bundle'])})")
    text += '\n> ' + ' · '.join(nav) + '\n\n' + summary + '\n\n'
    text += ('本文保留论文陈述、读者推断与待验证方案的区别；实验数字只适用于原文条件。'
             '原文和翻译来自已有 AI 辅助精读报告，已做来源匹配与编辑复核，'
             '不等于人工逐项核验或独立实验复现。所有页码均为 PDF 物理页。\n\n')
    text += '## 论文与阅读范围\n\n' + bibliography(report['bibliography']) + '\n\n'
    if entry['source_url']:
        text += f"[论文集中的原始 PDF]({entry['source_url']}) · "
    text += f"本地来源：`{entry['source']}`，共 {entry['pdf_pages']} 页。\n\n"
    text += f"本篇保留 {entry['reading_count']} 组原文精读和 {entry['evidence_count']} 条证据。"
    text += '截图为原始 PDF 的局部页选，不是重新生成的图表；未将整份 PDF 复制到博客。\n\n'
    by_id = {s['id']: s for s in report['sections']}
    ordered = [by_id[k] for k in SECTION_ORDER if k in by_id]
    ordered += [s for s in report['sections'] if s['id'] not in SECTION_ORDER]
    figures_used = set()
    for section in ordered:
        text += '## ' + clean(section['heading']).replace('\n', ' ') + '\n\n'
        text += clean(section['body']) + '\n\n'
        pages = ', '.join(str(p) for p in section.get('pages', []))
        ids = ', '.join(f"[{label(e)}](#evidence-{e.lower()})" for e in section.get('evidence_ids', []))
        basis = BASIS.get(section.get('basis'), str(section.get('basis', '未标注')))
        text += f'依据：{basis}；PDF 物理页 {pages or "未标注"}。'
        if ids:
            text += '证据：' + ids + '。'
        text += '\n\n'
        if section.get('citation_note'):
            text += '> 来源限定：' + clean(section['citation_note']) + '\n\n'
        if section.get('suggested_source_pages'):
            text += '> 建议另行对照的 PDF 物理页：' + str(section['suggested_source_pages']) + '。\n\n'
        role = {'architecture': 'mechanism', 'experiments': 'evaluation'}.get(section['id'])
        for shot in entry['screenshots']:
            if shot['role'] == role:
                text += render_screenshot(shot, entry)
                figures_used.add(shot['file'])
    for shot in entry['screenshots']:
        if shot['file'] not in figures_used:
            text += render_screenshot(shot, entry)
    text += '## 原文精读：从句子回到机制\n\n'
    text += '以下保留原文、忠实翻译、含义与边界；假设示例不作为论文实验结果。\n\n'
    if any(display_source(x['quote']) != x['quote'] for x in readings(data) + data['evidence']):
        text += ('原文中形如 `[U+0000]` 的标记表示 PDF 提取文本中的不可见控制字符，'
                 '不是论文中的变量，也不是补写的公式；原始报告不变，字形需对照原 PDF。\n\n')
    for i, reading in enumerate(readings(data), 1):
        pid = reading.get('id', f'P{i:03}')
        text += f"### {pid} · PDF 物理页 {reading['page']}\n\n"
        for heading, key in [('原文', 'quote'), ('忠实翻译', 'translation'),
                             ('技术含义与边界', 'interpretation'), ('常见误读', 'pitfall'),
                             ('读者构造的假设示例', 'example')]:
            if reading.get(key):
                value = str(reading[key]) if key == 'quote' else clean(reading[key])
                text += f'**{heading}**\n\n' + blockquote(value, literal=key == 'quote') + '\n\n'
    if report.get('open_questions'):
        text += '## 尚未解决的问题\n\n'
        text += '\n'.join(f'{i}. {clean(q)}' for i, q in enumerate(report['open_questions'], 1)) + '\n\n'
    text += '## 证据索引\n\n'
    text += '证据编号沿用原报告，便于将上文判断追溯到原句。引文匹配只验证位置，不证明解读正确。\n\n'
    for evidence in data['evidence']:
        eid = evidence['id']
        text += f'<a id="evidence-{html.escape(eid.lower(), quote=True)}"></a>\n\n'
        text += f"**{label(eid)} · PDF 物理页 {evidence['quote_page']}**\n\n"
        text += clean(evidence['claim']) + '\n\n' + blockquote(evidence['quote'], literal=True) + '\n\n'
        if evidence.get('page_reconciliation'):
            text += ('> 页码校正：原标注 ' + str(evidence.get('model_declared_pages'))
                     + '，引文在原输入块中唯一匹配 PDF 物理第' + str(evidence['quote_page'])
                     + '页；只校正位置，不代表解读正确。\n\n')
    coverage = data.get('coverage', {})
    text += '## 版本与使用边界\n\n'
    text += f"- 原 PDF SHA256：`{entry['pdf_sha256']}`。\n"
    text += f"- 报告来源：`{entry['engine']}`；此处按已完成报告整理，没有重新请求模型。\n"
    text += f"- OCR 记录页：{coverage.get('ocr_pages', [])}；低文本页：{coverage.get('low_text_pages', [])}。\n"
    text += ('- 本次新增页面截图，未据此自动重写原报告的数值、公式或实验结论；'
             '原有“未进行图像识别”限制仍适用于分析生成过程。\n'
             '- 截图与短引仅服务于论文评论，版权归原作者或出版方；正式发布前复查转载范围和授权。\n'
             '- 本文保留未核实的书目信息及研究问题，不用现代产品行为替代历史论文的证据。\n')
    if bibliography(report['bibliography']) != clean(report['bibliography']):
        text += ('\n<details>\n<summary>前序报告保留的书目与阅读过程记录</summary>\n\n'
                 + clean(report['bibliography']) + '\n\n</details>\n')
    return text


def render_screenshot(shot: dict, entry: dict) -> str:
    text = f"[![{label(entry['title'])}，PDF 物理页 {shot['page']} 原页截图]({shot['file']})]({shot['file']})\n\n"
    text += f"*原论文 PDF 物理页 {shot['page']}，版权归原作者或出版方。点击图片可查看原尺寸。"
    if shot['captions']:
        text += '页面文本中定位到：' + label('；'.join(shot['captions'])) + '。'
    text += '这是页面定位信息，不表示本次已复核图内所有数据。*\n\n'
    return text


def render_index(entries: list[dict], date_string: str) -> str:
    text = frontmatter({'title': f'【Paper】数据库论文原文精读：{len(entries)} 篇阅读索引',
                        'date': date_string + 'T12:00:00+08:00', 'slug': INDEX_SLUG,
                        'categories': ['数据库'], 'tags': ['论文精读', '数据库系统'],
                        'description': '从关系模型、优化器与执行引擎，到存储、事务与分布式共识的原文精读索引。',
                        'draft': True, 'toc': True, 'paper_reading': True})
    text += ('\n数据库论文需要放回各自的问题和假设中阅读：优化器讨论搜索空间和估计，'
             '执行引擎讨论 CPU 与内存访问，存储和分布式协议则需要分别明确持久化、并发与故障语义。'
             '这个索引按机制组织，不用加速比给不同工作排名。\n\n'
             '## 阅读路线\n\n'
             '先读关系模型与系统架构，建立语义和模块边界；随后沿优化器、执行引擎和存储索引向下深入。'
             '事务、共识和一致性需要独立对齐故障模型，不能将单机结论直接迁移到分布式系统。\n\n'
             '各篇先说明研究问题、机制和证据边界，再保留原文、翻译、误读与假设示例。'
             '书目信息和分析沿用本次已完成的报告，不代表重新复现了实验。'
             '全部文章当前为草稿；图片为原 PDF 选页截图，不包含整份论文。\n\n')
    groups = defaultdict(list)
    for entry in entries:
        groups[entry['topic']].append(entry)
    for name, group in groups.items():
        text += f'## {name}\n\n| 序号 | 论文精读 | PDF 页数 | 原文精读组数 |\n|---:|---|---:|---:|\n'
        for entry in group:
            text += (f"| {entry['number']} | [{label(entry['title'])}]({relref(entry['bundle'])}) "
                     f"| {entry['pdf_pages']} | {entry['reading_count']} |\n")
        text += '\n'
    text += '## 来源与限制\n\n'
    text += (f"共 {len(entries)} 篇、{sum(e['pdf_pages'] for e in entries)} 个 PDF 物理页，"
             f"保留 {sum(e['reading_count'] for e in entries)} 组原文解读、"
             f"{sum(e['evidence_count'] for e in entries)} 条证据和 "
             f"{sum(len(e['screenshots']) for e in entries)} 张截图。\n\n"
             '原文定位、OCR 转写和解释正确性是不同层次的检查。'
             '本文没有把 AI 编辑复核称为人工核验，尚未核实的出版信息保持未核实状态。'
             '正式发布前，仍需检查引用比例、图片使用权、公式和实验条件。\n')
    return text


def prepare(args: argparse.Namespace) -> tuple[list[dict], dict[str, bytes], dict]:
    site, source, reports = args.site.resolve(), args.source.resolve(), args.reports.resolve()
    if not (site / 'hugo.toml').is_file():
        raise ValueError('Expected an existing Hugo site')
    for root in (source, reports):
        if site.is_relative_to(root) or root.is_relative_to(site):
            raise ValueError('Site, source PDFs and reports must be disjoint')
    manifest = json.loads((reports / 'manifest.json').read_text(encoding='utf-8'))
    if not isinstance(manifest, list) or not manifest:
        raise ValueError('Empty or invalid manifest')
    if args.expected and len(manifest) != args.expected:
        raise ValueError(f'Expected {args.expected} papers, found {len(manifest)}')
    links = source_links(args.source_index, args.source_url)
    old_path = site / CATALOG
    old_catalog = json.loads(old_path.read_text(encoding='utf-8')) if (
        getattr(args, 'reuse_images', False) and old_path.exists()) else {}
    old_entries = {e['source']: e for e in old_catalog.get('papers', [])}
    index_bundle = args.date + '-' + INDEX_SLUG
    entries, documents, files = [], [], {}
    for number, item in enumerate(manifest, 1):
        print(f"Preparing {number}/{len(manifest)}: {item['source']}", flush=True)
        pdf = inside(source, item['source'])
        path = inside(reports, str(Path(item['report']).with_suffix('.json')))
        data = json.loads(path.read_text(encoding='utf-8'))
        sha = digest(pdf)
        if sha != item['sha256'] or sha != data['source_sha256']:
            raise ValueError(f"Source fingerprint mismatch: {item['source']}")
        if data['source'] != item['source']:
            raise ValueError('Report source mismatch')
        slug = slug_for(item['source'])
        name, english = topic(item['source'])
        entry = {'number': number, 'title': title(data), 'slug': slug,
                 'bundle': args.date + '-' + slug, 'source': item['source'],
                 'topic': name, 'topic_en': english, 'pdf_sha256': sha,
                 'report_sha256': digest(path), 'pdf_pages': item['pdf_pages'],
                 'source_url': links.get(normalized_name(pdf.name)),
                 'engine': data.get('model', data.get('engine', 'unknown')),
                 'reading_count': len(readings(data)), 'evidence_count': len(data['evidence'])}
        prior = old_entries.get(item['source'], {})
        reusable = (old_catalog.get('version') == VERSION and prior.get('pdf_sha256') == sha
                    and prior.get('report_sha256') == entry['report_sha256']
                    and prior.get('bundle') == entry['bundle'])
        with pymupdf.open(pdf) as doc:
            if doc.page_count != item['pdf_pages']:
                raise ValueError('PDF page count mismatch')
            entry['screenshots'] = prior['screenshots'] if reusable else screenshot_pages(doc, data['report']['sections'])
            for shot in entry['screenshots']:
                key = f"content/posts/{entry['bundle']}/{shot['file']}"
                cached = inside(site, key)
                if reusable and cached.is_file() and digest(cached) == old_catalog['generated_files'].get(key):
                    files[key] = cached.read_bytes()
                else:
                    pix = doc[shot['page'] - 1].get_pixmap(dpi=130, alpha=False)
                    files[key] = pix.tobytes('png')
        entries.append(entry)
        documents.append(data)
    if len({e['slug'] for e in entries}) != len(entries):
        raise ValueError('Slug collision; add an explicit stable override before importing')
    for i, (entry, data) in enumerate(zip(entries, documents)):
        article = render_article(entry, data, entries[i-1] if i else None,
                                 entries[i+1] if i+1 < len(entries) else None,
                                 index_bundle, args.date)
        files[f"content/posts/{entry['bundle']}/index.md"] = article.encode('utf-8')
    files[f'content/posts/{index_bundle}/index.md'] = render_index(entries, args.date).encode('utf-8')
    catalog = {'version': VERSION, 'date': args.date, 'draft': True, 'papers': entries,
               'generated_files': {key: hashlib.sha256(value).hexdigest() for key, value in files.items()}}
    return entries, files, catalog


def write_files(site: Path, files: dict[str, bytes], catalog: dict) -> None:
    catalog_path = site / CATALOG
    previous = json.loads(catalog_path.read_text(encoding='utf-8')) if catalog_path.exists() else {}
    # Preflight the complete set. Never overwrite a hand-edited article/image.
    for relative, content in files.items():
        path = inside(site, relative)
        if path.exists() and path.read_bytes() != content:
            if previous.get('generated_files', {}).get(relative) != digest(path):
                raise ValueError(f'Refusing to overwrite an unmanaged or edited file: {relative}')
    if previous and set(previous.get('generated_files', {})) != set(files):
        raise ValueError('Import file set changed; use a separate reviewed migration, not silent deletion')
    for relative, content in files.items():
        path = inside(site, relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists() or path.read_bytes() != content:
            path.write_bytes(content)
    catalog_path.parent.mkdir(parents=True, exist_ok=True)
    catalog_path.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--site', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--reports', type=Path, required=True)
    parser.add_argument('--source-index', type=Path)
    parser.add_argument('--source-url', help='Pinned public URL ending in /papers, matching source-index')
    parser.add_argument('--date', required=True, help='Article date in Asia/Shanghai, YYYY-MM-DD')
    parser.add_argument('--expected', type=int)
    parser.add_argument('--write', action='store_true', help='Without this flag, render in memory only')
    parser.add_argument('--reuse-images', action='store_true', help='Reuse checked images for unchanged PDF/report fingerprints')
    args = parser.parse_args()
    date.fromisoformat(args.date)
    if args.expected is not None and args.expected < 1:
        parser.error('--expected must be positive')
    if bool(args.source_index) != bool(args.source_url):
        parser.error('--source-index and --source-url must be provided together')
    entries, files, catalog = prepare(args)
    if args.write:
        write_files(args.site.resolve(), files, catalog)
    print(json.dumps({'written': args.write, 'papers': len(entries), 'articles': len(entries)+1,
                      'screenshots': sum(len(e['screenshots']) for e in entries),
                      'readings': sum(e['reading_count'] for e in entries),
                      'evidence': sum(e['evidence_count'] for e in entries),
                      'bytes': sum(map(len, files.values())), 'draft': True,
                      'unmapped_sources': [e['source'] for e in entries if not e['source_url']],
                      'topics': dict(Counter(e['topic'] for e in entries))}, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
