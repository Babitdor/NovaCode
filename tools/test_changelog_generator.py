"""Regression checks for the saved release archive. Run with Python unittest."""
import hashlib
import unittest

from tools.make_changelog_page import (
    _parse_version_from_filename,
    _version_key,
    build_summary_html,
    parse_release_entries,
    render_entry,
)


class ChangelogGeneratorTests(unittest.TestCase):
    def test_old_and_new_release_headers(self):
        for header in ('# NovaCode v1.0.54', '# NovaCode 1.0.54', '# Changelog - v1.0.54'):
            with self.subTest(header=header):
                entry = parse_release_entries(content=header + '\n## Fixed\n- A correction.\n')
                self.assertIsNotNone(entry)
                self.assertEqual(entry.version, '1.0.54')

    def test_wrapped_bullets_stay_together(self):
        entry = parse_release_entries(content='# NovaCode v1.0.54\n## Added\n- A **new** tool\n  across two lines.\n- Another tool.\n')
        self.assertEqual(entry.description, 'A new tool across two lines.')
        self.assertEqual(entry.details_html.count('<li>'), 2)
        self.assertIn('<strong>new</strong> tool across two lines.', entry.details_html)

    def test_native_controls_and_safe_content(self):
        entry = parse_release_entries(content='# NovaCode v1.0.54\n## Fixed\n- <script>alert(1)</script> & `code`\n')
        html = render_entry(entry)
        self.assertIn('<details ', html)
        self.assertIn('id="v1-0-54"', html)
        self.assertIn('id="v1-0-54"', html)
        self.assertNotIn('Link to v1.0.54', html)
        self.assertIn('&lt;script&gt;', html)
        self.assertNotIn('<script>', html)
        self.assertNotIn('&amp;lt;', html)
        self.assertNotIn('onclick=', html)

    def test_content_hash_matches_git_blob(self):
        content = '# NovaCode v1.0.54\n## Added\n- Tool.\n'
        entry = parse_release_entries(content=content)
        data = content.encode()
        self.assertEqual(entry.sha, hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest())

    def test_only_release_files_and_numeric_sort(self):
        self.assertIsNone(_parse_version_from_filename('HEADLESS-v1.0.54.md'))
        self.assertIsNone(_parse_version_from_filename('CHANGELOG-v1.0.54.md.bak'))
        self.assertEqual(_parse_version_from_filename('CHANGELOG-v1.0.54.md'), '1.0.54')
        self.assertGreater(_version_key('1.0.100'), _version_key('1.0.54'))
        entries = [parse_release_entries(content=f'# NovaCode v{v}\n- Change.') for v in ('1.0.100', '1.0.54')]
        self.assertIn('2 releases', build_summary_html(entries))


if __name__ == '__main__':
    unittest.main()
