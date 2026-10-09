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

    def test_settings_escape_special_characters(self):
        state = State('busy', EMPTY_PASSWORD_MD5)
        name = '/mock/Ä & "quoted" <folder>'
        state.share_dirs = [(name, 'subdirectory')]
        state.settings['nick'] = 'A & <B>'
        settings = ET.fromstring(state.settings_xml())
        self.assertEqual(settings.find('./share/directory').get('name'), name)
        self.assertEqual(settings.find('nick').text, 'A & <B>')

    def test_iso_catalog_is_split_into_folders(self):
        state = State('busy', EMPTY_PASSWORD_MD5)
        before = set(state.shares)
        added = state.add_iso_catalog(300)
        self.assertGreaterEqual(added, 250)
        isos = [s for i, s in state.shares.items() if i not in before]
        self.assertTrue(all(s['filename'].endswith('.iso') and s['filename'].startswith('/mock/isos/') for s in isos))
        self.assertEqual(len({s['filename'] for s in isos}), added)
        self.assertGreater(len({s['filename'].rsplit('/', 1)[0] for s in isos}), 50)
        self.assertIn(('/mock/isos', 'subdirectory'), state.share_dirs)
        self.assertIn('name="ubuntu"', state.directory_xml('/mock/isos'))
        self.assertIn('name="24.04.3"', state.directory_xml('/mock/isos/ubuntu'))
        self.assertIn('name="24.04.3" isfilesystem="true" type="4"', state.directory_xml('/mock/isos/ubuntu'))
        self.assertNotIn('<dir', state.directory_xml('/mock/isos/ubuntu/24.04.3'))
        self.assertEqual(state.add_iso_catalog(300), added)  # Repeating adds files, not duplicate share roots.
        self.assertEqual(state.share_dirs.count(('/mock/isos', 'subdirectory')), 1)

    def test_directory_listing_matches_core(self):
        state = State('busy', EMPTY_PASSWORD_MD5)
        roots = state.directory_xml(None)  # No parameter lists the roots, as File.listRoots() does.
        self.assertIn('<dir name="mock"', roots)
        self.assertNotIn(' path=', state.directory_xml('/mock'))  # Unix listing never sends a path.

    def test_setpriority_follows_share_rules(self):
        state = State('busy', EMPTY_PASSWORD_MD5)
        first, second = list(state.shares)[:2]
        self.assertEqual(state.action('setpriority', {'id': [str(first)], 'priority': ['300']}), f"ok: set to {state.shares[first]['priority']}")
        self.assertNotEqual(state.shares[first]['priority'], 300)  # Out of 1..250: ignored.
        self.assertEqual(state.action('setpriority', {'id': [str(first)], 'priority': ['5']}), 'ok: set to 5')
        self.assertEqual(state.action('setpriority', {'id': ['1']}), 'error: invalid id')
        for sid in state.shares:
            state.shares[sid]['priority'] = 1
        state.shares[first]['priority'] = 900
        state.action('setpriority', {'id': [str(second)], 'priority': ['250']})
        self.assertEqual(state.shares[second]['priority'], 100)  # Total is capped at 1000.

    def test_settargetdir_validation_and_power_download(self):
        state = State('busy', EMPTY_PASSWORD_MD5)
        self.assertEqual(state.action('settargetdir', {'id': ['105']}), 'error: missing dir')
        self.assertEqual(state.action('settargetdir', {'id': ['105'], 'dir': ['../x']}), 'error: invalid dir')
        self.assertEqual(state.action('settargetdir', {'id': ['105'], 'dir': ['C:x']}), 'error: invalid dir')
        self.assertEqual(state.action('settargetdir', {'id': ['999999'], 'dir': ['ok']}), 'error: invalid id')
        self.assertEqual(state.action('settargetdir', {'id': ['105'], 'dir': ['ok']}), 'ok')
        self.assertEqual(state.action('setpowerdownload', {'id': ['105'], 'Powerdownload': ['22']}), 'ok: set to 22')
        self.assertEqual(state.action('setpowerdownload', {'id': ['999999'], 'powerdownload': ['1']}), 'error: invalid id')

    def test_setsettings_share_list_like_core(self):
        state = State('empty', EMPTY_PASSWORD_MD5)
        state.action('setsettings', {'countshares': ['3'], 'sharedirectory1': ['/a'], 'sharesub1': ['True'],
                                     'sharedirectory2': ['/b'], 'sharesub2': ['False'], 'sharedirectory3': ['/c']})
        # Incoming directory stays, entry 3 lacks sharesub and is skipped, N..1 order.
        self.assertEqual(state.share_dirs, [('/mock/incoming', 'subdirectory'), ('/b', 'singledirectory'), ('/a', 'subdirectory')])
        state.action('setsettings', {'countshares': ['1'], 'sharedirectory1': ['/mock/incoming/'], 'sharesub1': ['False']})
        self.assertEqual(state.share_dirs, [('/mock/incoming', 'singledirectory')])  # Existing path only changes its mode.
        state.action('setsettings', {'Nickname': ['x'], 'MaxUpload': ['5']})
        self.assertEqual((state.settings['nick'], state.settings['maxupload']), ('x', '5'))

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
