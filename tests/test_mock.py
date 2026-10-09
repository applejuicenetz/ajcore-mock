import hashlib
import http.client
import threading
import time
import unittest
import urllib.parse
import xml.etree.ElementTree as ET
import zlib
from http.server import ThreadingHTTPServer
from mock_core import (State, Handler, EMPTY_PASSWORD_MD5, SEARCH_RESULTS, SEARCH_RESULT_INTERVAL,
                       SERVER_LOGIN_DELAY, SESSION_TTL, CANCEL_DELAY, file_hash, parse_int)
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

    def test_busy_uploads_use_only_core_states(self):
        state = State('busy', EMPTY_PASSWORD_MD5)
        rows = ET.fromstring('<r>' + ''.join(state.upload_xml(u) for u in state.uploads.values()) + '</r>')
        self.assertEqual({int(u.get('status')) for u in rows}, {1, 2})
        self.assertEqual({int(u.get('directstate')) for u in rows}, {0, 1, 2})
        for u in rows:
            if u.get('status') == '2':  # Queued slots have no transfer window and no speed.
                self.assertEqual((u.get('uploadfrom'), u.get('actualuploadposition'), u.get('uploadto'), u.get('speed')),
                                 ('-1', '-1', '-1', '0'))
            else:
                self.assertTrue(int(u.get('uploadfrom')) <= int(u.get('actualuploadposition')) <= int(u.get('uploadto')))
            self.assertTrue(float(u.get('loaded')) == -1.0 or 0.0 <= float(u.get('loaded')) <= 100.0)

    def test_every_reference_points_to_an_existing_object(self):
        state = State('busy', EMPTY_PASSWORD_MD5)
        for u in state.uploads.values():
            self.assertIn(u['shareid'], state.shares)
        for d in state.downloads.values():
            self.assertTrue(d['shareid'] == -1 or d['shareid'] in state.shares, d)
            self.assertIsNotNone(state.find_object(d['id']))
        for u in state.users.values():
            self.assertIn(u['downloadid'], state.downloads)
        for e in state.entries.values():
            self.assertIn(e['searchid'], state.searches)
        ids = [i for i, _kind, _xml in state.object_rows()]
        self.assertEqual(len(ids), len(set(ids)))  # one ID space for all object types

    def test_download_share_follows_core(self):
        state = State('busy', EMPTY_PASSWORD_MD5)
        by_name = {d['filename']: d for d in state.downloads.values()}
        self.assertEqual(by_name['suchend.bin']['shareid'], -1)  # no data yet, no share
        ubuntu = next(d for n, d in by_name.items() if n.startswith('ubuntu'))
        share = state.shares[ubuntu['shareid']]
        self.assertEqual((share['size'], share['checksum'], share['short']), (ubuntu['size'], ubuntu['hash'], ubuntu['filename']))
        self.assertEqual(share['filename'], f"/mock/temp/{ubuntu['temporaryfilenumber']}.data")
        state.action('canceldownload', {'id': [str(ubuntu['id'])]})
        state.downloads[ubuntu['id']]['cancel_at'] = 0
        state.tick()
        self.assertEqual((ubuntu['status'], ubuntu['shareid']), (17, -1))
        self.assertNotIn(share['id'], state.shares)

    def test_hash_identifies_name_and_size(self):
        state = State('busy', EMPTY_PASSWORD_MD5)
        keys = {}
        for s in state.shares.values():
            keys.setdefault(s['checksum'], set()).add(s['size'])
        self.assertTrue(all(len(sizes) == 1 for sizes in keys.values()))
        self.assertNotEqual(file_hash('a', 1), file_hash('a', 2))

    def test_big_directory_has_upload_on_late_entry(self):
        state = State('busy', EMPTY_PASSWORD_MD5)
        state.add_big_directory(450)
        in_dir = [x for x in state.shares.values() if x['filename'].startswith(state.BIG_DIR + '/')]
        self.assertEqual(len(in_dir), 450)
        self.assertTrue(any(state.shares[u['shareid']]['filename'].startswith(state.BIG_DIR) for u in state.uploads.values()))

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
        roots = state.directory_xml(None)  # No parameter lists the file system roots.
        self.assertIn('<dir name="/" isfilesystem="true" type="4"/>', roots)
        self.assertIn('<dir name="mock"', state.directory_xml('/'))
        for shared in ('archive', 'catalog', 'incoming', 'isos'):
            self.assertIn(f'<dir name="{shared}"', state.directory_xml('/mock'))
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
        self.assertEqual(state.action('setpowerdownload', {'id': ['105'], 'powerdownload': ['5']}), 'ok: set to 22')  # ignored
        self.assertEqual(state.action('setpowerdownload', {'id': ['105'], 'powerdownload': ['491']}), 'ok: set to 22')
        self.assertEqual(state.action('setpowerdownload', {'id': ['105'], 'powerdownload': ['0']}), 'ok: set to 0')
        self.assertEqual(state.action('setpowerdownload', {'id': ['999999'], 'powerdownload': ['1']}), 'error: invalid id')

    def test_setsettings_share_list_like_core(self):
        state = State('empty', EMPTY_PASSWORD_MD5)
        state.action('setsettings', {'countshares': ['3'], 'sharedirectory1': ['/a'], 'sharesub1': ['True'],
                                     'sharedirectory2': ['/b'], 'sharesub2': ['False'], 'sharedirectory3': ['/c']})
        # Incoming directory stays, entry 3 lacks sharesub and is skipped, N..1 order.
        self.assertEqual(state.share_dirs, [('/mock/incoming', 'subdirectory'), ('/b', 'singledirectory'), ('/a', 'subdirectory')])
        state.action('setsettings', {'countshares': ['1'], 'sharedirectory1': ['/mock/incoming/'], 'sharesub1': ['False']})
        self.assertEqual(state.share_dirs, [('/mock/incoming', 'singledirectory')])  # Existing path only changes its mode.
        state.action('setsettings', {'Nickname': ['x'], 'MaxUpload': ['5000']})
        self.assertEqual((state.settings['nick'], state.settings['maxupload']), ('x', 5000))

    def test_processlink_target_directory_like_core(self):
        state = State('empty', EMPTY_PASSWORD_MD5)
        link = 'ajfsp://file|a.iso|' + 'a' * 32 + '|10/'
        state.action('processlink', {'link': [link], 'subdir': ['Linux/ISOs']})
        state.action('processlink', {'link': [link.replace('a.iso', 'b.iso').replace('a' * 32, 'b' * 32)], 'subdir': ['../x']})
        state.action('processlink', {'link': [link.replace('a.iso', 'c.iso').replace('a' * 32, 'c' * 32)]})
        targets = {d['filename']: d['targetdirectory'] for d in state.downloads.values()}
        self.assertEqual(targets, {'a.iso': 'Linux/ISOs', 'b.iso': '', 'c.iso': ''})

    def test_synthetic_servers(self):
        state = State('busy', EMPTY_PASSWORD_MD5)
        self.assertTrue(all(s['host'].endswith('.example') for s in state.servers.values()))

    def test_share_object_lookup(self):
        state = State('busy', EMPTY_PASSWORD_MD5)
        share_id = next(iter(state.shares))
        self.assertIn(f'id="{share_id}"', state.share_row(state.shares[share_id]))

    def test_actions(self):
        state = State('busy', EMPTY_PASSWORD_MD5)
        self.assertEqual(state.action('pausedownload', {'id': ['105']}), 'ok')
        self.assertEqual(state.downloads[105]['status'], 18)
        self.assertEqual(state.action('resumedownload', {'id': ['105']}), 'ok')
        self.assertEqual(state.downloads[105]['status'], 0)

    def test_download_actions_validate_ids_and_states(self):
        state = State('busy', EMPTY_PASSWORD_MD5)
        share = next(iter(state.shares))
        for name in ('pausedownload', 'resumedownload', 'canceldownload'):
            self.assertEqual(state.action(name, {'id': ['99999']}), 'error: invalid id')
            self.assertEqual(state.action(name, {'id': [str(share)]}), 'error: invalid id')  # not a download
            self.assertEqual(state.action(name, {'id': ['abc']}), '')  # unparsable numbers are skipped
        self.assertEqual(state.action('pausedownload', {'id': ['105'], 'id1': ['999'], 'id2': ['111']}),
                         'ok' 'error: invalid id' 'ok')
        cancelled = next(d for d in state.downloads.values() if d['filename'] == 'abgebrochen.rar')
        state.action('resumedownload', {'id': [str(cancelled['id'])]})
        self.assertEqual(cancelled['status'], 17)  # resume only reverts a pause
        done = next(d for d in state.downloads.values() if d['status'] == 14)
        state.action('canceldownload', {'id': [str(done['id'])]})
        self.assertEqual(done['status'], 14)

    def test_cancel_passes_through_status_15(self):
        state = State('busy', EMPTY_PASSWORD_MD5)
        state.action('canceldownload', {'id': ['105']})
        self.assertEqual(state.downloads[105]['status'], 15)
        state.downloads[105]['cancel_at'] = time.time() - 1
        state.tick()
        self.assertEqual(state.downloads[105]['status'], 17)
        self.assertEqual(state.downloads[105]['users'], [])

    def test_clean_rename_and_misc_errors(self):
        state = State('busy', EMPTY_PASSWORD_MD5)
        self.assertEqual(state.action('renamedownload', {'id': ['105']}), 'error: missing name')
        self.assertEqual(state.action('renamedownload', {'id': ['1'], 'name': ['x']}), 'error: invalid id')
        self.assertEqual(state.action('renamedownload', {'id': ['105'], 'name': ['neu.bin']}), 'ok')
        self.assertEqual(state.downloads[105]['filename'], 'neu.bin')
        state.downloads[117]['status'] = 13
        state.action('cleandownloadlist', {})
        self.assertNotIn(117, state.downloads)
        self.assertEqual(state.action('cancelsearch', {'id': ['1']}), 'error: invalid id')
        self.assertEqual(state.action('search', {'search': ['']}), 'failure')
        self.assertEqual(state.action('processlink', {}), 'failure: link missing')
        self.assertEqual(state.action('processlink', {'link': ['ajfsp://other|x']}), 'failure: incorrect link')
        self.assertEqual(state.action('processlink', {'link': ['https://x.example']}), '')
        self.assertEqual(state.action('processlink', {'link': ['ajfsp://server|s.example|9855/']}), '')
        self.assertEqual(state.action('processlink', {'link': ['ajfsp://server|s.example|x']}), 'failure: incorrect link')
        self.assertEqual(state.action('processlink', {'link': ['ajfsp://file|a.iso|' + 'a' * 32 + '|x']}), 'failure: incorrect link')
        shared = next(s for s in state.shares.values() if s['short'] == 'ajcore.jar')
        self.assertEqual(state.action('processlink', {'link': [f"ajfsp://file|x|{shared['checksum']}|{shared['size']}"]}),
                         'already downloaded')
        count = len(state.servers)
        state.action('processlink', {'link': ['ajfsp://server|s.example|9855']})
        self.assertEqual(len(state.servers), count)  # a known server is only refreshed

    def test_processlink_sources_are_unqueried_link_sources(self):
        state = State('empty', EMPTY_PASSWORD_MD5)
        state.action('processlink', {'link': ['ajfsp://file|a.iso|' + 'a' * 32 + '|10|192.0.2.1:9850|192.0.2.2:1:192.0.2.3:2|bad']})
        download = next(iter(state.downloads.values()))
        self.assertEqual(len(download['users']), 2)
        self.assertEqual({state.users[u]['source'] for u in download['users']}, {1})

    def test_server_login_is_asynchronous_and_active_server_stays(self):
        state = State('busy', EMPTY_PASSWORD_MD5)
        other = [i for i in state.servers if i != state.connected_server][0]
        self.assertEqual(state.action('serverlogin', {}), 'failure')
        self.assertEqual(state.action('serverlogin', {'id': ['x']}), 'failure: For input string: "x"')
        self.assertEqual(state.action('serverlogin', {'id': [str(other)]}), 'ok')
        self.assertEqual((state.connected_server, state.connecting), (-1, other))
        self.assertIn(f'tryconnecttoserver="{other}"', state.information_xml())
        self.assertEqual(state.action('removeserver', {'id': [str(other)]}), 'ok')
        self.assertIn(other, state.servers)  # connecting server is not removed
        state.connecting_since -= SERVER_LOGIN_DELAY + 1
        state.tick()
        self.assertEqual((state.connected_server, state.connecting), (other, -1))
        self.assertEqual(state.action('removeserver', {'id': ['1']}), 'error: invalid id')
        gone = [i for i in state.servers if i != other][0]
        self.assertEqual(state.action('removeserver', {'id': [str(gone)]}), 'ok')
        self.assertNotIn(gone, state.servers)

    def test_setpassword_and_setsettings_clamps(self):
        state = State('empty', EMPTY_PASSWORD_MD5)
        self.assertEqual(state.action('setpassword', {}), 'error: unknown password')
        self.assertEqual(state.action('setpassword', {'newpassword': ['abc']}), 'error: invalid md5checksum')
        self.assertTrue(state.action('setpassword', {'newpassword': ['z' * 32]}).startswith('error: '))
        new = hashlib.md5(b'x').hexdigest().upper()
        self.assertEqual(state.action('setpassword', {'newpassword': [new]}), 'ok')
        self.assertEqual(state.password_md5, new.lower())
        state.action('setsettings', {'maxupload': ['1'], 'maxconnections': ['1'], 'maxnewconnectionsperturn': ['999'],
                                     'maxdownload': ['-5'], 'autoconnect': ['yes'], 'nickname': ['']})
        s = state.settings
        self.assertEqual((s['maxupload'], s['maxconnections'], s['maxnewconnectionsperturn'], s['maxdownload']),
                         (3072, 30, 200, 0))
        self.assertEqual((s['autoconnect'], s['nick']), ('false', 'mocknick'))

    def test_parse_int_matches_parse_int(self):
        self.assertEqual(parse_int('-12'), -12)
        for bad in ('', ' 1', '1 ', '1_0', '2147483648', '1.5'):
            with self.assertRaises(ValueError):
                parse_int(bad)
        self.assertEqual(parse_int('2147483648', 64), 2147483648)

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

    def test_busy_search_results_overlap_shares_and_downloads(self):
        state = State('busy', EMPTY_PASSWORD_MD5)
        shared = {s['checksum'] for s in state.shares.values()}
        loading = {d['hash'] for d in state.downloads.values()}
        states = {}
        for e in state.entries.values():
            if e['searchid'] == min(state.searches):
                name = e['names'][0][0]
                states[name] = (e['checksum'] in shared, e['checksum'] in loading)
        self.assertEqual(states, {
            'debian-13.7.0-amd64-netinst.iso': (True, True), 'debian-live-xfce.iso': (True, False),
            'debian-notes.txt': (False, True), 'debian-unknown.iso': (False, False)})


class XmlContractTests(unittest.TestCase):
    def test_partlist_is_gap_free_and_merged(self):
        state = State('busy', EMPTY_PASSWORD_MD5)
        for d in state.downloads.values():
            parts = ET.fromstring(state.download_partlist_xml(d)).findall('part')
            starts = [int(p.get('fromposition')) for p in parts]
            self.assertEqual(starts[0], 0)
            self.assertEqual(starts, sorted(set(starts)))
            self.assertTrue(all(s < d['size'] or d['size'] == 0 for s in starts))
            kinds = [int(p.get('type')) for p in parts]
            self.assertTrue(all(a != b for a, b in zip(kinds, kinds[1:])))
        ubuntu = next(d for d in state.downloads.values() if d['filename'].startswith('ubuntu'))
        parts = ET.fromstring(state.download_partlist_xml(ubuntu)).findall('part')
        self.assertEqual([(p.get('fromposition'), p.get('type')) for p in parts],
                         [('0', '-1'), (str(ubuntu['ready']), str(len(ubuntu['users'])))])
        finished = next(d for d in state.downloads.values() if d['status'] == 14)
        self.assertEqual([p.get('type') for p in ET.fromstring(state.download_partlist_xml(finished)).findall('part')], ['-1'])

    def test_userpartlist_only_local_or_missing(self):
        state = State('busy', EMPTY_PASSWORD_MD5)
        for u in state.users.values():
            types = {int(p.get('type')) for p in ET.fromstring(state.user_partlist_xml(u)).findall('part')}
            self.assertTrue(types <= {-1, 0})

    def test_ids_list_is_complete_and_modified_filters(self):
        state = State('busy', EMPTY_PASSWORD_MD5)
        ids = ET.fromstring(state.modified_xml({'ids'}))
        self.assertEqual(len(ids.findall('./ids/serverid')), len(state.servers))
        self.assertEqual(len(ids.findall('./ids/uploadid')), len(state.uploads))
        self.assertEqual(len(ids.findall('./ids/downloadid')), len(state.downloads))
        self.assertEqual(len(ids.findall('./ids/downloadid/userid')), len(state.users))
        self.assertEqual([c.tag for c in ET.fromstring(state.modified_xml(set()))], ['time'])
        self.assertEqual([c.tag for c in ET.fromstring(state.modified_xml({'bogus'}))], ['time'])
        full = ET.fromstring(state.modified_xml(None))
        tags = {c.tag for c in full}
        self.assertTrue({'time', 'ids', 'download', 'user', 'upload', 'server', 'search', 'searchentry', 'information', 'networkinfo'} <= tags)
        self.assertNotIn('share', tags)
        only = {c.tag for c in ET.fromstring(state.modified_xml({'informations'}))}
        self.assertEqual(only, {'time', 'information', 'networkinfo'})
        self.assertIn('searchentry', {c.tag for c in ET.fromstring(state.modified_xml({'search'}))})

    def test_modified_timestamp_and_removed_queue(self):
        state = State('busy', EMPTY_PASSWORD_MD5)
        session = state.sessions.setdefault(7, {'ip': '127.0.0.1', 'last': time.time(), 'removed': []})
        future = int(time.time() * 1000) + 60_000
        quiet = ET.fromstring(state.modified_xml({'down'}, future))
        self.assertEqual(quiet.findall('download'), [])
        state.action('cleandownloadlist', {})
        state.downloads[105]['status'] = 14
        state.action('cleandownloadlist', {})
        state.sync_objects()
        self.assertIn(105, session['removed'])
        removed = ET.fromstring(state.modified_xml({'ids'}, 0, session)).findall('./removed/object')
        self.assertIn('105', [o.get('id') for o in removed])
        self.assertEqual(ET.fromstring(state.modified_xml({'ids'}, 0, session)).findall('./removed/object'), [])  # reading clears
        self.assertIsNone(ET.fromstring(state.modified_xml({'ids'}, 0, session)).find('ids'))
        state.downloads[117]['status'] = 18
        state.sync_objects()
        changed = ET.fromstring(state.modified_xml({'down'}, int(time.time() * 1000) - 5000, session)).findall('download')
        self.assertIn('117', [d.get('id') for d in changed])

    def test_getobject_knows_every_type(self):
        state = State('busy', EMPTY_PASSWORD_MD5)
        tags = {}
        for oid, kind, _ in state.object_rows():
            tags[kind] = ET.fromstring(f"<r>{state.find_object(oid)}</r>")[0].tag
        self.assertEqual(tags, {k: k for k in ('share', 'download', 'user', 'upload', 'server', 'search', 'searchentry', 'information', 'networkinfo')})

    def test_networkinfo_filesize_is_gigabytes_with_two_decimals(self):
        state = State('busy', EMPTY_PASSWORD_MD5)
        net = ET.fromstring(state.networkinfo_row())
        self.assertRegex(net.get('filesize'), r'^\d+\.\d\d$')
        self.assertLess(float(net.get('filesize')), 1_000_000)  # GB, not bytes


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.state = State('busy', EMPTY_PASSWORD_MD5)
        Handler.state = cls.state
        cls.httpd = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.httpd.verbose = False
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def get(self, path, method='GET', headers=None, body=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        conn.request(method, path, body=body, headers=headers if headers is not None else {'X-AppleJuice-Password': EMPTY_PASSWORD_MD5})
        r = conn.getresponse()
        data = r.read()
        conn.close()
        return r, data

    def test_auth_redirects_and_public_pages(self):
        r, body = self.get('/xml/information.xml', headers={})
        self.assertEqual((r.status, r.getheader('Location'), body), (302, '/wrongpassword', b''))
        r, _ = self.get('/xml/information.xml', headers={'X-AppleJuice-Password': 'bad'})
        self.assertEqual(r.status, 302)
        r, _ = self.get(f'/xml/information.xml?password={EMPTY_PASSWORD_MD5}', headers={})
        self.assertEqual(r.status, 200)
        r, _ = self.get(f'/xml/information.xml?password={EMPTY_PASSWORD_MD5}', headers={'X-AppleJuice-Password': 'bad'})
        self.assertEqual(r.status, 302)  # a present header wins
        r, body = self.get('/wrongpassword', headers={})
        self.assertEqual((r.status, r.getheader('Content-Type').split(';')[0]), (200, 'text/html'))
        self.assertIn(b'wrong password', body)
        r, body = self.get('/help', headers={})
        self.assertEqual(r.status, 200)
        r, _ = self.get('/nothing/here')
        self.assertEqual((r.status, r.getheader('Location')), (302, '/help'))
        r, _ = self.get('/xml/unknown.xml')
        self.assertEqual((r.status, r.getheader('Location')), (302, '/help'))
        r, _ = self.get('/function/unknown')
        self.assertEqual((r.status, r.getheader('Location')), (302, '/help'))

    def test_options_is_unauthenticated(self):
        r, _ = self.get('/anything', method='OPTIONS', headers={})
        self.assertEqual(r.status, 200)
        self.assertEqual(r.getheader('Access-Control-Allow-Methods'), 'GET, POST, OPTIONS')

    def test_session_flow(self):
        r, body = self.get('/xml/getsession.xml')
        sid = int(ET.fromstring(body).find('session').get('id'))
        self.assertGreater(sid, 0)
        r, body = self.get(f'/xml/modified.xml?session={sid}&filter=ids&timestamp=0')
        root = ET.fromstring(body)
        self.assertIsNotNone(root.find('removed'))
        self.assertIsNone(root.find('ids'))
        r, body = self.get(f'/xml/modified.xml?session={sid + 1}&filter=ids')
        self.assertEqual((r.status, r.getheader('Location')), (302, '/wrongsession'))
        r, body = self.get('/xml/modified.xml?filter=ids')
        self.assertIsNotNone(ET.fromstring(body).find('ids'))
        self.state.sessions[sid]['last'] -= SESSION_TTL + 1
        r, _ = self.get(f'/xml/modified.xml?session={sid}')
        self.assertEqual(r.status, 302)

    def test_zip_mode_is_zlib(self):
        r, body = self.get('/xml/getobject.xml?id=999999&mode=ZIP')
        self.assertEqual(r.getheader('Content-Type'), 'application/zlib')
        self.assertEqual(zlib.decompress(body), b'error: invalid id')
        self.assertEqual(int(r.getheader('Content-Length')), len(body))
        r, body = self.get('/xml/information.xml', method='OPTIONS', headers={})
        self.assertEqual(r.status, 200)
        r, body = self.get('/function/search?search=zz&mode=zip')
        self.assertEqual(r.getheader('Content-Type'), 'text/html')
        self.assertIn(b'<html>', zlib.decompress(body))

    def test_content_types_and_empty_html(self):
        r, body = self.get('/function/search?search=')
        self.assertEqual((r.getheader('Content-Type').split(';')[0], body), ('text/html', b'failure'))
        r, body = self.get('/function/pausedownload?id=105')
        self.assertEqual((r.getheader('Content-Type').split(';')[0], body), ('text/xml', b'ok'))
        r, body = self.get('/function/search?search=probe')
        self.assertEqual(body, b'<html><body></body></html>')

    def test_post_form_only_with_exact_content_type(self):
        form = urllib.parse.urlencode({'id': '999999'})
        headers = {'X-AppleJuice-Password': EMPTY_PASSWORD_MD5}
        r, body = self.get('/xml/getobject.xml', 'POST', {**headers, 'Content-Type': 'application/x-www-form-urlencoded'}, form)
        self.assertEqual(body, b'error: invalid id')
        known = next(iter(self.state.shares))
        form = urllib.parse.urlencode({'id': str(known)})
        r, body = self.get('/xml/getobject.xml?id=999999', 'POST', {**headers, 'Content-Type': 'application/x-www-form-urlencoded'}, form)
        self.assertIn(b'<share', body)  # form overrides query
        r, body = self.get('/xml/getobject.xml', 'POST', {**headers, 'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8'}, form)
        self.assertEqual(body, b'error: invalid id')  # charset suffix: body is ignored

    def test_partlist_errors_and_setsettings_abort(self):
        r, body = self.get('/xml/downloadpartlist.xml?id=424242')
        self.assertTrue(body.startswith(b'failure: '))
        r, body = self.get('/xml/userpartlist.xml?id=105')
        self.assertTrue(body.startswith(b'failure: '))
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        conn.request('GET', '/function/setsettings?maxupload=abc', headers={'X-AppleJuice-Password': EMPTY_PASSWORD_MD5})
        with self.assertRaises((http.client.RemoteDisconnected, ConnectionError)):
            conn.getresponse()
        conn.close()


if __name__ == '__main__':
    unittest.main()
