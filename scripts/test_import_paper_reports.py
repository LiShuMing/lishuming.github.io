"""Offline importer checks. Fixtures never touch the real paper collection."""

import argparse
import json
from pathlib import Path
import tempfile
import unittest

import pymupdf

import import_paper_reports as importer
from validate_paper_posts import Page, local_target


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.site, self.source, self.reports = [self.root / k for k in ('site', 'pdfs', 'reports')]
        for directory in (self.site, self.source, self.reports):
            directory.mkdir()
        (self.site / 'hugo.toml').touch()
        pdf = self.source / 'example.pdf'
        with pymupdf.open() as doc:
            doc.new_page().insert_text((72, 72), 'Figure 1. Example mechanism')
            doc.new_page().insert_text((72, 72), 'Table 1. Evaluation conditions')
            doc.save(pdf)
        self.data = {'source': 'example.pdf', 'source_sha256': importer.digest(pdf),
                     'model': 'test-model', 'coverage': {},
                     'report': {'title': 'Example 深度研究报告', 'bibliography': 'Source, 2020.',
                                'executive_summary': 'Example summary.',
                                'sections': [{'id': 'architecture', 'heading': 'Mechanism',
                                              'body': 'First.\\n\\nSecond.', 'basis': 'paper',
                                              'pages': [1], 'evidence_ids': ['E01']},
                                             {'id': 'experiments', 'heading': 'Evaluation',
                                              'body': 'Conditions.', 'basis': 'inference',
                                              'pages': [2], 'evidence_ids': ['E01']}],
                                'open_questions': ['What remains?']},
                     'evidence': [{'id': 'E01', 'quote_page': 1, 'pages': [1],
                                   'claim': 'Source claim.', 'quote': 'a < b; *not emphasis*'}],
                     'original_passages': [{'id': 'P001', 'page': 1, 'quote': 'Original * text.'}],
                     'close_readings': {'readings': [{'passage_id': 'P001', 'translation': 'Translation.',
                                                      'interpretation': 'Interpretation.',
                                                      'pitfall': 'Boundary.', 'example': 'Hypothesis.'}]}}
        (self.reports / 'report.json').write_text(json.dumps(self.data), encoding='utf-8')
        self.manifest = [{'source': 'example.pdf', 'sha256': self.data['source_sha256'],
                          'pdf_pages': 2, 'report': 'report.html'}]
        (self.reports / 'manifest.json').write_text(json.dumps(self.manifest), encoding='utf-8')
        self.args = argparse.Namespace(site=self.site, source=self.source, reports=self.reports,
                                       source_index=None, source_url=None, date='2026-10-22', expected=1)

    def prepare(self):
        return importer.prepare(self.args)

    def test_prepare_keeps_readings_evidence_and_draft(self):
        entries, files, catalog = self.prepare()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]['reading_count'], 1)
        self.assertEqual(len(entries[0]['screenshots']), 2)
        path = f"content/posts/{entries[0]['bundle']}/index.md"
        md = files[path].decode()
        for value in ('draft: true', 'math: true', 'Translation.', 'Interpretation.',
                      'Boundary.', 'Hypothesis.', 'First.\n\nSecond.', '#evidence-e01'):
            self.assertIn(value, md)
        self.assertIn('a &lt; b; *not emphasis*', md)
        self.assertEqual(catalog['version'], 1)
        self.assertFalse((self.site / 'content').exists())

    def test_codex_schema_preserves_all_fields(self):
        self.data.pop('original_passages')
        self.data['close_readings'] = [{'page': 1, 'quote': 'Original', 'translation': 'Translation',
                                       'interpretation': 'Interpretation', 'pitfall': 'Pitfall', 'example': 'Example'}]
        reading = importer.readings(self.data)[0]
        self.assertEqual(reading['id'], 'P001')
        self.assertEqual(reading['quote'], 'Original')
        self.assertEqual(reading['pitfall'], 'Pitfall')

    def test_write_is_idempotent(self):
        _, files, catalog = self.prepare()
        importer.write_files(self.site, files, catalog)
        importer.write_files(self.site, files, catalog)
        for relative, content in files.items():
            self.assertEqual((self.site / relative).read_bytes(), content)

    def test_hand_edit_is_not_overwritten(self):
        entries, files, catalog = self.prepare()
        importer.write_files(self.site, files, catalog)
        path = self.site / f"content/posts/{entries[0]['bundle']}/index.md"
        path.write_text('Hand edited', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'edited file'):
            importer.write_files(self.site, files, catalog)
        self.assertEqual(path.read_text(), 'Hand edited')

    def test_unmanaged_existing_file_is_not_overwritten(self):
        _, files, catalog = self.prepare()
        relative = next(iter(files))
        path = self.site / relative
        path.parent.mkdir(parents=True)
        path.write_bytes(b'unmanaged')
        with self.assertRaisesRegex(ValueError, 'unmanaged'):
            importer.write_files(self.site, files, catalog)

    def test_changed_source_is_rejected(self):
        (self.source / 'example.pdf').write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'fingerprint'):
            self.prepare()

    def test_paths_cannot_escape_roots(self):
        with self.assertRaisesRegex(ValueError, 'escapes'):
            importer.inside(self.source, '../secret')

    def test_overlapping_roots_rejected(self):
        self.args.reports = self.site / 'reports'
        with self.assertRaisesRegex(ValueError, 'disjoint'):
            self.prepare()

    def test_count_is_enforced(self):
        self.args.expected = 2
        with self.assertRaisesRegex(ValueError, 'Expected 2'):
            self.prepare()

    def test_markdown_source_paths_and_punctuation(self):
        index = self.root / 'README.md'
        index.write_text('- [Example](papers/consensus/a-(b):test.pdf) (2020)\n', encoding='utf-8')
        links = importer.source_links(index, 'https://example.com/commit/papers')
        self.assertEqual(links[importer.normalized_name('a-(b)_test.pdf')],
                         'https://example.com/commit/papers/consensus/a-%28b%29%3Atest.pdf')

    def test_learned_cardinality_is_not_misclassified_as_sketch(self):
        self.assertEqual(importer.topic('probabilistic-counting/neurocard.pdf')[0], '统计与基数估计')

    def test_source_index_handles_encoded_spaces_and_external_pdfs(self):
        index = self.root / 'README.md'
        index.write_text('- [With Spaces](./papers/with%20spaces.pdf) (2020)\n'
                         '- [A Paper](https://example.com/original.pdf) (2020)\n', encoding='utf-8')
        links = importer.source_links(index, 'https://example.com/commit/papers')
        self.assertEqual(links[importer.normalized_name('with spaces.pdf')],
                         'https://example.com/commit/papers/with%20spaces.pdf')
        self.assertEqual(links[importer.normalized_name('a-paper.pdf')], 'https://example.com/original.pdf')

    def test_manifest_has_no_absolute_machine_paths(self):
        _, _, catalog = self.prepare()
        self.assertNotIn(str(self.root), json.dumps(catalog))

    def test_changed_file_set_requires_review(self):
        _, files, catalog = self.prepare()
        importer.write_files(self.site, files, catalog)
        subset = dict(files)
        subset.pop(next(iter(subset)))
        with self.assertRaisesRegex(ValueError, 'file set changed'):
            importer.write_files(self.site, subset, catalog)

    def test_captions_are_not_a_claim_of_visual_verification(self):
        entries, _, _ = self.prepare()
        shot = importer.render_screenshot(entries[0]['screenshots'][0], entries[0])
        self.assertIn('不表示本次已复核图内所有数据', shot)
        self.assertIn('[![', shot)

    def test_bibliography_separates_operational_details(self):
        self.assertEqual(importer.bibliography('Authors. Title. 方法：API attempt failed.'), 'Authors. Title.')

    def test_image_reuse_requires_matching_fingerprints(self):
        _, files, catalog = self.prepare()
        importer.write_files(self.site, files, catalog)
        self.args.reuse_images = True
        _, cached, _ = self.prepare()
        self.assertEqual(files, cached)

    def test_html_parser_decodes_quotes_and_collects_anchors(self):
        page = Page('<a id="evidence-e01"></a><p>a &lt; b</p><img src="source.png">')
        self.assertEqual(page.ids, {'evidence-e01'})
        self.assertEqual(''.join(page.text), 'a < b')
        self.assertEqual(page.images, ['source.png'])

    def test_rendered_links_cannot_escape_build(self):
        build = self.root / 'build'
        with self.assertRaisesRegex(ValueError, 'escapes'):
            local_target(build, build / 'index.html', '../private.txt')

    def test_source_parentheses_and_latex_are_verbatim(self):
        original = r'FUNCTION-RULE (see above); a < b; \(x\); *word*'
        markup = importer.blockquote(original, literal=True)
        self.assertIn('<pre><code>', markup)
        self.assertEqual(''.join(Page(markup).text), original)

    def test_relational_projection_is_not_a_markdown_link(self):
        self.assertIn(r'π\[A,B\]', importer.blockquote('π[A,B](Sel(C>0)(R))'))

    def test_blank_source_lines_do_not_end_the_raw_html_block(self):
        source = 'First paragraph.\n\nSecond paragraph.\n \n[formula](S)'
        markup = importer.blockquote(source, literal=True)
        self.assertNotIn('\n', markup)
        self.assertEqual(''.join(Page(markup).text), source)

    def test_font_controls_are_explicit_not_silently_corrected(self):
        markup = importer.blockquote('set \x00 and variable \x01', literal=True)
        self.assertEqual(''.join(Page(markup).text), 'set [U+0000] and variable [U+0001]')


if __name__ == '__main__':
    unittest.main()
