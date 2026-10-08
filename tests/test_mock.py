import unittest
import xml.etree.ElementTree as ET
from mock_core import State, EMPTY_PASSWORD_MD5
from share_index import populate

class MockTests(unittest.TestCase):
    def test_index_matches_api(self):
        state = State('busy', EMPTY_PASSWORD_MD5)
        index = populate(state)
        self.assertEqual(len(index), 3500000)
        files = ET.fromstring(index).findall('file')
        shares = ET.fromstring(state.share_xml()).findall('./shares/share')
        self.assertEqual(len(files), len(shares))
        self.assertEqual({f.get('name') for f in files}, {s.get('filename') for s in shares})
        self.assertTrue(files[0].findall('subhash'))
        print(f'index={len(index)} bytes; shares={len(shares)}; API={len(state.share_xml().encode())} bytes')

    def test_synthetic_servers(self):
        state = State('busy', EMPTY_PASSWORD_MD5)
        self.assertTrue(all(s['host'].endswith('.example') for s in state.servers.values()))
        self.assertNotIn('chimera', state.server_xml().lower())
        self.assertNotIn('apple-deluxe', state.server_xml().lower())

    def test_share_object_lookup(self):
        state = State('busy', EMPTY_PASSWORD_MD5)
        share_id = next(iter(state.shares))
        self.assertIn(f'id="{share_id}"', state.share_row(state.shares[share_id]))

    def test_actions(self):
        state = State('busy', EMPTY_PASSWORD_MD5)
        state.action('pausedownload', {'id': ['105']})
        self.assertEqual(state.downloads[105]['status'], 18)
        state.action('resumedownload', {'id': ['105']})
        self.assertEqual(state.downloads[105]['status'], 0)

if __name__ == '__main__':
    unittest.main()
