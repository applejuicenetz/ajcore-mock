import unittest
import xml.etree.ElementTree as ET
from mock_core import State, EMPTY_PASSWORD_MD5, SEARCH_RESULTS, SEARCH_RESULT_INTERVAL
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

    def test_running_search_delivers_results_and_finishes(self):
        state = State('empty', EMPTY_PASSWORD_MD5)
        sid = state.add_search('probe')
        search = state.searches[sid]
        self.assertEqual((search['running'], search['found'], search['open']), ('true', 0, SEARCH_RESULTS))
        search['started'] -= SEARCH_RESULT_INTERVAL * 2.5
        state.tick()
        self.assertEqual((search['running'], search['found']), ('true', 2))
        self.assertEqual(len([e for e in state.entries.values() if e['searchid'] == sid]), 2)
        state.tick()  # without time passing no further result arrives
        self.assertEqual(search['found'], 2)
        search['started'] -= SEARCH_RESULT_INTERVAL * 10
        state.tick()
        self.assertEqual((search['running'], search['found'], search['open']), ('false', SEARCH_RESULTS, 0))
        self.assertEqual(len([e for e in state.entries.values() if e['searchid'] == sid]), SEARCH_RESULTS)
        self.assertIn('running="false"', state.search_xml())

    def test_cancelled_search_stops_delivering(self):
        state = State('empty', EMPTY_PASSWORD_MD5)
        sid = state.add_search('probe')
        state.searches[sid]['started'] -= SEARCH_RESULT_INTERVAL * 1.5
        state.tick()
        state.action('cancelsearch', {'id': [str(sid)]})
        state.searches[sid]['started'] -= SEARCH_RESULT_INTERVAL * 10
        state.tick()
        self.assertEqual((state.searches[sid]['running'], state.searches[sid]['found']), ('false', 1))

    def test_finished_fixture_search_is_unchanged(self):
        state = State('busy', EMPTY_PASSWORD_MD5)
        before = {sid: dict(s) for sid, s in state.searches.items()}
        state.tick()
        self.assertEqual(before, {sid: dict(s) for sid, s in state.searches.items()})

if __name__ == '__main__':
    unittest.main()
