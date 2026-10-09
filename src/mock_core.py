#!/usr/bin/env python3
"""Standalone synthetic appleJuice Core HTTP/XML service for API client tests.

Python standard library only. Supported endpoints include settings, information,
shares, directories, downloads, uploads, searches and state-changing functions.
No connections to real Core instances or P2P servers are made.

Examples:
    python3 src/mock_core.py
    python3 src/mock_core.py --scenario empty --shareidx-bytes 0
    python3 src/mock_core.py --password test --shareidx-output runtime/shareidx.xml
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import re
import threading
import time
import urllib.parse
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from xml.sax.saxutils import escape, quoteattr
from pathlib import Path
from share_index import DEFAULT_INDEX_BYTES, populate, write

DEFAULT_PORT = 19851
EMPTY_PASSWORD_MD5 = hashlib.md5(b"").hexdigest()

# Download states: 0 loading/searching, 14 complete, 17 canceled, 18 paused
# Source states: 1 unqueried, 5 queued, 7 transferring, 13 paused
# Upload states: 1 active transfer, 2 queue. directstate: 0 unknown, 1 direct, 2 indirect.
# Object IDs share one space. Shares, downloads, users, uploads, servers, searches and
# search entries are addressable through /xml/getobject.xml.


def now_ms() -> int:
    return int(time.time() * 1000)


def attrs(**values) -> str:
    return " ".join(f"{k}={quoteattr(str(v))}" for k, v in values.items())


SERVER_LOGIN_DELAY = 2.0  # seconds until a started server login counts as connected
SESSION_TTL = 30.0  # seconds without a validated modified.xml access
# Actions whose Core handler sets no Content-Type and therefore answers text/html.
HTML_ACTIONS = {"search", "serverlogin", "processlink", "setsettings"}


def file_hash(name: str, size: int) -> str:
    """Deterministic fixture hash. Core identifies a file by hash and size, so both feed the digest."""
    return hashlib.md5(f"{name}:{size}".encode()).hexdigest()


CANCEL_DELAY = 2.0  # seconds a cancelled download stays in status 15 before it becomes 17
_INT_PATTERN = re.compile(r"[+-]?[0-9a-fA-F]+\Z")


def parse_int(text: str, bits: int = 32, radix: int = 10) -> int:
    """Strict integer parsing: no whitespace, no underscores, range checked (32 or 64 bit)."""
    suffix = "" if radix == 10 else f" under radix {radix}"
    valid = _INT_PATTERN.match(text) is not None
    if valid:
        try:
            value = int(text, radix)
        except ValueError:
            valid = False
    if not valid or not -(2 ** (bits - 1)) <= value < 2 ** (bits - 1):
        raise ValueError(f'For input string: "{text}"{suffix}')
    return value


def round_half_up(value: float) -> int:
    return math.floor(value + 0.5)


class Redirect(Exception):
    """Core answers with an empty 302 and a Location header."""

    def __init__(self, location: str):
        super().__init__(location)
        self.location = location


class AbortConnection(Exception):
    """Core closes the TCP connection without any answer (unparsed numbers in setsettings)."""


SEARCH_RESULT_INTERVAL = 3.0  # seconds between synthetic results of a running search
SEARCH_RESULTS = 4  # results a running search delivers before it finishes

class State:
    """Core state. Protect all access with self.lock."""

    def __init__(self, scenario: str, password_md5: str):
        self.lock = threading.RLock()
        self.password_md5 = password_md5
        self.started = now_ms()
        self.next_id = 100
        self.settings = {
            "nick": "mocknick",
            "port": 9850,
            "xmlport": DEFAULT_PORT,
            "autoconnect": "true",
            "maxupload": 262144,
            "maxdownload": 1048576,
            "maxconnections": 250,
            "maxsourcesperfile": 500,
            "speedperslot": 30,
            "maxnewconnectionsperturn": 50,
            "incomingdirectory": "/mock/incoming/",
            "temporarydirectory": "/mock/temp/",
        }
        self.share_dirs = [("/mock/incoming", "subdirectory")]
        self.credits = 0
        self.servers: dict[int, dict] = {}
        self.downloads: dict[int, dict] = {}
        self.users: dict[int, dict] = {}
        self.uploads: dict[int, dict] = {}
        self.shares: dict[int, dict] = {}
        self.searches: dict[int, dict] = {}
        self.entries: dict[int, dict] = {}
        self.connected_server = -1
        self.connected_since = 0
        self.firewalled = "false"
        self.exited = False
        self.last_tick = time.time()
        self.connecting = -1
        self.connecting_since = 0.0
        self.sessions: dict[int, dict] = {}
        self.stamps: dict = {}
        self.snapshot: dict = {}
        getattr(self, f"scenario_{scenario}")()
        # information and networkinfo are addressable objects too; allocated last so scenario IDs stay stable.
        self.information_id = self.new_id()
        self.networkinfo_id = self.new_id()
        self.sync_objects()

    # ---- Scenarios -------------------------------------------------------
    def new_id(self) -> int:
        self.next_id += 1
        return self.next_id

    def add_server(self, name, host, port, connected=False):
        sid = self.new_id()
        self.servers[sid] = {"id": sid, "name": name, "host": host, "port": port,
                             "lastseen": now_ms() if connected else 0,
                             "connectiontry": 0}
        if connected:
            self.connected_server = sid
            self.connected_since = now_ms() - 3_600_000
        return sid

    def add_download(self, filename, size, ready=0, status=0, pdl=0, target="", sources=(), md5=None, partshare=False):
        did = self.new_id()
        digest = md5 or file_hash(filename, size)
        # The ID after the download is reserved for its part-file share so fixture IDs stay stable.
        # shareid is -1 until the download has a share.
        sid = self.new_id()
        shareid = -1
        if partshare:
            shareid = sid
            self.shares[sid] = {"id": sid, "filename": f"{self.settings['temporarydirectory']}{did}.data",
                                "short": filename, "size": size, "checksum": digest, "priority": 1,
                                "lastasked": 0, "askcount": 0, "searchcount": 0}
        self.downloads[did] = {
            "id": did, "shareid": shareid, "size": size, "ready": ready,
            "hash": digest,
            "status": status, "powerdownload": pdl, "filename": filename,
            "temporaryfilenumber": did, "targetdirectory": target, "users": [],
        }
        for status_u, speed, nick in sources:
            self.add_source(did, status_u, speed, nick)
        return did

    def add_source(self, did, status_u, speed, nick, source=4):
        download = self.downloads[did]
        uid = self.new_id()
        self.users[uid] = {
            "id": uid, "downloadid": did, "status": status_u, "speed": speed, "source": source,
            "nickname": nick, "version": "0.35.185.93", "os": 1, "directstate": 1,
            "queueposition": 0 if status_u == 7 else 4, "from": -1, "to": -1,
            "pos": -1, "filename": download["filename"],
        }
        download["users"].append(uid)
        if status_u == 7:
            self.assign_chunk(self.users[uid], download)
        return uid

    CHUNK = 8 * 1024 * 1024

    def assign_chunk(self, user, download):
        """Assign an active source a byte range beyond completed data."""
        taken = sum(1 for u in download["users"] if self.users[u]["from"] >= 0)
        start = min(download["ready"] + taken * self.CHUNK, max(download["size"] - 1, 0))
        user["from"] = start
        user["to"] = min(start + self.CHUNK, download["size"])
        user["pos"] = start

    def scenario_empty(self):
        self.add_server("Sample server A", "server-a.example", 9855, connected=True)
        self.firewalled = "false"

    def scenario_busy(self):
        self.credits = 4_300_000_000
        self.add_server("Sample server A", "server-a.example", 9855, connected=True)
        self.add_server("Sample server with a long name for wrapping tests", "server-b.example", 9855)
        self.add_server("", "server-c.example", 7001)
        self.add_server("Sample server D", "server-d.example", 9855) # Synthetic endpoint.
        self.add_download("ubuntu-26.04.1-desktop-arm64-with-a-really-long-file-name-for-wrapping-tests.iso",
                          4_154_798_080, ready=1_200_000_000, pdl=0,
                          sources=[(7, 180_000, "alice"), (7, 95_000, "bob"), (5, 0, "carol"), (1, 0, "dave")],
                          partshare=True)
        self.add_download("debian-13.7.0-amd64-netinst.iso", 735_358_976, ready=100_000_000, pdl=12,
                          sources=[(7, 400_000, "erin")])
        self.add_download("Konzert [2024] ÄÖÜ ß <test> & \"quotes\".mkv", 1_500_000_000, ready=300_000_000,
                          sources=[(5, 0, "frank")], target="Videos")
        self.add_download("pausiert.zip", 52_000_000, ready=10_000_000, status=18)
        self.add_download("abgebrochen.rar", 220_000_000, ready=1_000_000, status=17)
        self.add_download("fertig.tar.gz", 8_000_000, ready=8_000_000, status=14)
        self.add_download("suchend.bin", 99_000_000, ready=0, sources=[])
        # (nick, status, speed, directstate, remote load percent). Queued slots have no positions or speed.
        uploads = [("alice", 1, 180_000, 1, 37.5), ("gerd", 2, 0, 0, -1.0), ("heidi", 2, 0, 1, 0.0),
                   ("ivan", 2, 0, 2, 12.0), ("judy", 1, 64_000, 2, 100.0), ("karl", 2, 0, 2, -1.0),
                   ("lena", 1, 32_000, 1, 62.25)]
        names = ["debian-13.7.0-amd64-netinst.iso", "ajcore.jar", "linux.iso", "movie.mkv",
                 "verbinde.bin", "indirekt.bin", "fehler.bin"]
        for i, (nick, status, speed, direct, loaded) in enumerate(uploads):
            uid = self.new_id()
            sid = self.new_id()
            fname = names[i]
            size = 735_358_976 if i == 0 else 100_000_000 * (i + 1)  # netinst matches the download and search fixtures
            self.shares[sid] = {"id": sid, "filename": f"/mock/incoming/{fname}", "short": fname,
                                "size": size, "checksum": file_hash(fname, size),
                                "priority": 1, "lastasked": now_ms() - 60_000, "askcount": i, "searchcount": i * 2}
            active = status == 1
            self.uploads[uid] = {"id": uid, "shareid": sid, "version": "0.35.185.93", "os": 1 + i % 3,
                                 "status": status, "directstate": direct, "priority": 100 - i * 10,
                                 "nick": nick, "from": 0 if active else -1, "pos": 2_000_000 * (i + 1) if active else -1,
                                 "to": 10_000_000 * (i + 1) if active else -1,
                                 "lastconnection": now_ms(), "speed": speed, "loaded": loaded}
        for n in range(5):
            sid = self.new_id()
            self.shares[sid] = {"id": sid, "filename": f"/mock/incoming/archiv-{n}.zip", "short": f"archiv-{n}.zip",
                                "size": 12_000_000 * (n + 1), "checksum": hashlib.md5(f"a{n}".encode()).hexdigest(),
                                "priority": 1 + n % 3, "lastasked": 0, "askcount": 0, "searchcount": 0}
        self.add_search("debian", running=False, results=[
            ("debian-13.7.0-amd64-netinst.iso", 735_358_976, [("debian-13.7.0-amd64-netinst.iso", 12), ("debian.iso", 3)]),
            ("debian-live-xfce.iso", 3_200_000_000, [("debian-live-xfce.iso", 5)]),
            ("debian-notes.txt", 12_345, [("debian-notes.txt", 1)]),
            ("debian-unknown.iso", 650_000_000, [("debian-unknown.iso", 2)]),
        ])
        # Search result states, matched by checksum: netinst is shared (upload fixture) and downloading,
        # xfce is only shared, notes is only downloading, debian-unknown.iso is neither.
        sid = self.new_id()
        self.shares[sid] = {"id": sid, "filename": "/mock/incoming/debian-live-xfce.iso", "short": "debian-live-xfce.iso",
                            "size": 3_200_000_000, "checksum": file_hash("debian-live-xfce.iso", 3_200_000_000),
                            "priority": 1, "lastasked": 0, "askcount": 0, "searchcount": 0}
        self.add_download("debian-notes.txt", 12_345, ready=2_000, sources=[(7, 1_000, "mallory")])
        self.firewalled = "true"

    ISO_ROOT = "/mock/isos"
    ISO_DISTROS = {
        "ubuntu": (["20.04.6", "22.04.5", "24.04.3", "25.04", "25.10"],
                   ["desktop-amd64", "live-server-amd64", "live-server-arm64", "desktop-arm64"]),
        "debian": (["11.11.0", "12.12.0", "13.7.0"],
                   ["amd64-netinst", "amd64-DVD-1", "arm64-netinst", "i386-netinst", "amd64-xfce-live"]),
        "fedora": (["41-1.4", "42-1.1", "43-1.6"], ["Workstation-x86_64", "Server-x86_64", "KDE-x86_64", "Everything-aarch64"]),
        "archlinux": (["2025.06.01", "2025.09.01", "2025.12.01"], ["x86_64", "aarch64"]),
        "linuxmint": (["21.3", "22", "22.1"], ["cinnamon-64bit", "mate-64bit", "xfce-64bit"]),
        "opensuse": (["leap-15.6", "leap-16.0", "tumbleweed"], ["DVD-x86_64", "NET-x86_64", "Live-KDE-x86_64"]),
        "almalinux": (["8.10", "9.6", "10.0"], ["x86_64-dvd", "x86_64-boot", "x86_64-minimal"]),
        "rockylinux": (["8.10", "9.6", "10.0"], ["x86_64-dvd", "x86_64-boot", "x86_64-minimal"]),
        "manjaro": (["24.2", "25.0"], ["kde", "gnome", "xfce"]),
        "kali": (["2025.2", "2025.3", "2025.4"], ["installer-amd64", "live-amd64", "netinst-amd64"]),
        "popos": (["22.04", "24.04"], ["amd64-intel", "amd64-nvidia"]),
        "gentoo": (["20250601", "20250901", "20251130"], ["install-amd64-minimal", "livegui-amd64"]),
        "freebsd": (["13.5", "14.3", "15.0"], ["amd64-disc1", "amd64-dvd1", "arm64-aarch64-disc1"]),
        "alpine": (["3.20.8", "3.21.5", "3.22.2"], ["standard-x86_64", "extended-x86_64", "virt-aarch64"]),
        "nixos": (["24.11", "25.05"], ["gnome-x86_64", "minimal-x86_64", "plasma6-x86_64"]),
        "zorin": (["17.3", "18"], ["Core-64-bit", "Lite-64-bit"]),
        "centos-stream": (["9", "10"], ["x86_64-dvd1", "x86_64-boot", "aarch64-dvd1", "ppc64le-dvd1"]),
        "oraclelinux": (["8.10", "9.6", "10.0"], ["x86_64-dvd", "x86_64-boot", "aarch64-dvd"]),
        "elementary": (["7.1", "8.0", "8.1"], ["amd64", "amd64-nvidia"]),
        "endeavouros": (["2025.03.19", "2025.06.14", "2025.11.24"], ["x86_64", "aarch64"]),
        "mxlinux": (["23.6", "25"], ["x64", "ahs-x64", "fluxbox-x64"]),
        "void": (["20250202", "20250616"], ["x86_64-live", "x86_64-musl-live", "aarch64-live"]),
        "tails": (["6.14", "6.17", "7.0"], ["amd64"]),
        "slackware": (["15.0", "current"], ["install-dvd", "install-dvd-aarch64"]),
        "truenas": (["24.10", "25.04"], ["scale", "core"]),
        "proxmox": (["8.4", "9.0"], ["ve", "backup-server", "mail-gateway"]),
        "qubes": (["4.2.4", "4.3"], ["x86_64"]),
        "rhel-ubi": (["9.6", "10.0"], ["x86_64-dvd", "aarch64-dvd"]),
        "ubuntu-mate": (["22.04", "24.04", "25.04"], ["desktop-amd64", "desktop-arm64"]),
        "kubuntu": (["22.04", "24.04", "25.04"], ["desktop-amd64"]),
        "xubuntu": (["22.04", "24.04", "25.04"], ["desktop-amd64", "minimal-amd64"]),
        "lubuntu": (["22.04", "24.04", "25.04"], ["desktop-amd64", "desktop-arm64"]),
        "debian-edu": (["12.12.0", "13.7.0"], ["amd64-netinst", "amd64-BD-1"]),
        "ubuntu-budgie": (["22.04", "24.04", "25.04"], ["desktop-amd64"]),
        "ubuntu-studio": (["22.04", "24.04", "25.04"], ["dvd-amd64"]),
        "ubuntu-server": (["22.04.5", "24.04.3"], ["live-amd64", "live-arm64", "live-s390x", "live-ppc64el"]),
        "raspios": (["2025-05-13", "2025-10-01"], ["arm64-lite", "arm64-desktop", "armhf-lite"]),
        "clonezilla": (["3.2.1", "3.2.2", "3.3.0"], ["amd64", "i686"]),
        "gparted": (["1.6.0", "1.7.0"], ["amd64", "i686", "arm64"]),
        "memtest86plus": (["7.00", "7.20"], ["x64", "x86"]),
        "systemrescue": (["11.03", "12.00", "12.02"], ["amd64", "i686"]),
        "opnsense": (["24.7", "25.1", "25.7"], ["dvd-amd64", "vga-amd64", "nano-amd64"]),
        "pfsense": (["2.7.2", "2.8.0", "2.8.1"], ["CE-amd64"]),
        "netbsd": (["10.0", "10.1"], ["amd64", "evbarm-aarch64", "i386"]),
        "openbsd": (["7.6", "7.7", "7.8"], ["install-amd64", "install-arm64"]),
    }

    def add_iso_catalog(self, count: int = 300) -> int:
        """Add deterministic ISO shares below ISO_ROOT/<distribution>/<release>/ and share the root recursively."""
        names = []
        for distro, (releases, flavours) in self.ISO_DISTROS.items():
            for release in releases:
                for flavour in flavours:
                    names.append((distro, release, f"{distro}-{release}-{flavour}.iso"))
        names.sort(key=lambda n: hashlib.md5("/".join(n).encode()).hexdigest())  # Deterministic mixed order.
        added = 0
        for distro, release, name in names[:count]:
            digest = hashlib.md5(name.encode()).digest()
            size = (400 + int.from_bytes(digest[:3], "big") % 4200) * 1_000_000
            sid = self.new_id()
            self.shares[sid] = {"id": sid, "filename": f"{self.ISO_ROOT}/{distro}/{release}/{name}", "short": name,
                                "size": size, "checksum": file_hash(name, size), "priority": 1 + digest[3] % 4,
                                "lastasked": 0, "askcount": digest[4] % 40, "searchcount": digest[5] % 25}
            added += 1
        if added and (self.ISO_ROOT, "subdirectory") not in self.share_dirs:
            self.share_dirs.append((self.ISO_ROOT, "subdirectory"))
        return added

    BIG_DIR = "/mock/archive"

    def add_big_directory(self, count: int = 450) -> int:
        """Add a flat directory larger than one client page; one upload targets a late entry."""
        last = 0
        for n in range(count):
            name = f"bulk-{n:04d}.dat"
            digest = hashlib.md5(name.encode()).digest()
            sid = self.new_id()
            self.shares[sid] = {"id": sid, "filename": f"{self.BIG_DIR}/{name}", "short": name, "size": 1_000_000 + n,
                                "checksum": digest.hex(), "priority": 1, "lastasked": 0, "askcount": 0, "searchcount": 0}
            last = sid
        uid = self.new_id()
        self.uploads[uid] = {"id": uid, "shareid": last, "version": "0.35.185.93", "os": 1, "status": 2, "directstate": 1,
                             "priority": 50, "nick": "mona", "from": -1, "pos": -1, "to": -1,
                             "lastconnection": now_ms(), "speed": 0, "loaded": -1.0}
        if (self.BIG_DIR, "subdirectory") not in self.share_dirs:
            self.share_dirs.append((self.BIG_DIR, "subdirectory"))
        return count

    def scenario_firewalled(self):
        self.scenario_busy()
        self.firewalled = "true"

    def scenario_disconnected(self):
        self.scenario_empty()
        self.connected_server = -1
        self.connected_since = 0
        self.credits = -50_000_000

    def add_search(self, text, running=True, results=()):
        sid = self.new_id()
        self.searches[sid] = {"id": sid, "text": text, "open": SEARCH_RESULTS if running else 0,
                              "sum": 0 if running else SEARCH_RESULTS, "running": "true" if running else "false"}
        if running:
            # Running searches receive synthetic results over time, see State.advance_searches.
            self.searches[sid]["started"] = time.time()
            self.searches[sid]["delivered"] = 0
        for name, size, names in results:
            eid = self.new_id()
            self.entries[eid] = {"id": eid, "searchid": sid, "size": size,
                                 "checksum": file_hash(name, size), "names": names}
        self.searches[sid]["found"] = len(results)
        return sid

    # ---- Simulation ------------------------------------------------------
    def advance_searches(self, now):
        """Deliver one synthetic result per SEARCH_RESULT_INTERVAL; finish after SEARCH_RESULTS results."""
        for s in self.searches.values():
            if s["running"] != "true":
                continue
            due = min(SEARCH_RESULTS, int((now - s["started"]) / SEARCH_RESULT_INTERVAL))
            for n in range(s["delivered"] + 1, due + 1):
                name = f"{s['text']}-result-{n}.iso"
                size = 1_000_000 * n
                eid = self.new_id()
                self.entries[eid] = {"id": eid, "searchid": s["id"], "size": size,
                                     "checksum": file_hash(f"{s['id']}:{name}", size),
                                     "names": [(name, n)]}
            s["delivered"] = due
            s["found"] = due
            s["sum"] = due
            s["open"] = SEARCH_RESULTS - due
            if due >= SEARCH_RESULTS:
                s["running"] = "false"
                s["open"] = 0

    def finish_cancel(self, d):
        """Finish a cancel: sources and the part-file share go away."""
        d["status"] = 17
        for u in d["users"]:
            self.users.pop(u, None)
        d["users"] = []
        self.drop_part_share(d)

    def drop_part_share(self, d):
        share = self.shares.get(d["shareid"])
        if share is not None and share["filename"].startswith(self.settings["temporarydirectory"]):
            del self.shares[d["shareid"]]
        d["shareid"] = -1

    def tick(self):
        now = time.time()
        dt = now - self.last_tick
        self.last_tick = now
        for sid in [i for i, v in self.sessions.items() if now - v["last"] > SESSION_TTL]:
            del self.sessions[sid]
        if self.connecting != -1 and now - self.connecting_since >= SERVER_LOGIN_DELAY:
            if self.connecting in self.servers:
                self.connected_server = self.connecting
                self.connected_since = now_ms()
                self.servers[self.connecting]["lastseen"] = now_ms()
            self.connecting = -1
        self.advance_searches(now)
        for d in self.downloads.values():
            if d["status"] == 15 and now >= d.get("cancel_at", 0):
                self.finish_cancel(d)
                continue
            if d["status"] != 0:
                continue
            speed = 0
            for uid in d["users"]:
                u = self.users[uid]
                if u["status"] == 7:
                    speed += u["speed"]
            if speed:
                d["ready"] = min(d["size"], d["ready"] + int(speed * dt))
                for uid in d["users"]:
                    u = self.users[uid]
                    if u["status"] != 7:
                        continue
                    u["pos"] = min(u["to"], u["pos"] + int(u["speed"] * dt))
                    if u["pos"] >= u["to"] and u["to"] < d["size"]:
                        u["from"] = -1
                        self.assign_chunk(u, d)
                if d["ready"] > 0 and d["shareid"] == -1:
                    self.attach_part_share(d)  # The share appears with the first data.
                if d["ready"] >= d["size"]:
                    d["status"] = 14
                    for uid in d["users"]:
                        self.users.pop(uid, None)
                    d["users"] = []
                    share = self.shares.get(d["shareid"])
                    if share is not None:  # The finished file becomes a regular share in Incoming.
                        share["filename"] = self.settings["incomingdirectory"] + d["filename"]
                    d["shareid"] = -1
        dl, _ul = self.totals()
        self.credits += int(dl * dt * 0.2)
        self.sync_objects()

    def totals(self):
        active = {d["id"] for d in self.downloads.values() if d["status"] == 0}
        dl = sum(u["speed"] for u in self.users.values() if u["status"] == 7 and u["downloadid"] in active)
        ul = sum(u["speed"] for u in self.uploads.values() if u["status"] == 1)
        return dl, ul

    # ---- Object tracking (modified.xml timestamps and removed lists) ----
    def object_rows(self):
        """Yield (id, kind, xml) for every addressable object; shares carry no xml here."""
        for i in self.shares:
            yield i, "share", None
        for kind, coll, fn in (("download", self.downloads, self.download_xml), ("user", self.users, self.user_xml),
                               ("upload", self.uploads, self.upload_xml), ("server", self.servers, self.server_row),
                               ("search", self.searches, self.search_row),
                               ("searchentry", self.entries, self.entry_xml)):
            for i, o in coll.items():
                yield i, kind, fn(o)
        yield self.information_id, "information", self.information_row()
        yield self.networkinfo_id, "networkinfo", self.networkinfo_row()

    def sync_objects(self):
        """Stamp new/changed objects and tell every session about vanished ones."""
        now = now_ms()
        current = {}
        for i, kind, xml in self.object_rows():
            current[i] = (kind, xml)
            before = self.snapshot.get(i)
            if before is None or before[1] != xml:
                self.stamps[i] = now
        for i in self.snapshot:
            if i not in current:
                self.stamps.pop(i, None)
                for session in self.sessions.values():
                    session["removed"].append(i)
        self.snapshot = current

    # ---- XML -------------------------------------------------------------
    def information_row(self):
        dl, ul = self.totals()
        return f"<information {attrs(credits=self.credits, sessionupload=123456, sessiondownload=654321, uploadspeed=ul, downloadspeed=dl, openconnections=len(self.users), maxuploadpositions=50)}/>"

    def networkinfo_row(self):
        return (f"<networkinfo {attrs(users=450, files=4191120, filesize='1449.57', firewalled=self.firewalled, ip='203.0.113.7', tryconnecttoserver=self.connecting, connectedwithserverid=self.connected_server, connectedsince=self.connected_since, paused='false')}>\n"
                f"<welcomemessage>Mock Core: welcome to the synthetic network</welcomemessage></networkinfo>")

    def information_xml(self):
        return self.information_row() + "\n" + self.networkinfo_row()

    @staticmethod
    def server_row(s):
        return f"<server {attrs(id=s['id'], name=s['name'], host=s['host'], lastseen=s['lastseen'], port=s['port'], connectiontry=s['connectiontry'])}/>"

    def server_xml(self):
        return "\n".join(self.server_row(s) for s in self.servers.values())

    def ids_xml(self):
        """Complete server/upload/download id lists, as sent when a request carries no session."""
        out = ["<ids>"]
        out += [f'<serverid id="{i}"/>' for i in self.servers]
        out += [f'<uploadid id="{i}"/>' for i in self.uploads]
        for d in self.downloads.values():
            out.append(f'<downloadid id="{d["id"]}">')
            out += [f'<userid id="{u}"/>' for u in d["users"]]
            out.append("</downloadid>")
        out.append("</ids>")
        return "\n".join(out)

    def download_xml(self, d):
        return f"<download {attrs(id=d['id'], shareid=d['shareid'], hash=d['hash'], size=d['size'], status=d['status'], powerdownload=d['powerdownload'], temporaryfilenumber=d['temporaryfilenumber'], filename=d['filename'], targetdirectory=d['targetdirectory'], ready=d['ready'])}/>"

    def user_xml(self, u):
        return f"<user {attrs(id=u['id'], source=u.get('source', 4), downloadid=u['downloadid'], status=u['status'], directstate=u['directstate'], downloadfrom=u['from'], downloadto=u['to'], actualdownloadposition=u['pos'], speed=u['speed'], version=u['version'], operatingsystem=u['os'], queueposition=u['queueposition'], powerdownload=0, filename=u['filename'], nickname=u['nickname'])}/>"

    def upload_xml(self, u):
        return f"<upload {attrs(id=u['id'], shareid=u['shareid'], version=u['version'], operatingsystem=u['os'], status=u['status'], directstate=u['directstate'], priority=u['priority'], nick=u['nick'], uploadfrom=u['from'], actualuploadposition=u['pos'], uploadto=u['to'], lastconnection=u['lastconnection'], speed=u['speed'], loaded=u['loaded'])}/>"

    @staticmethod
    def search_row(s):
        return f"<search {attrs(id=s['id'], searchtext=s['text'], opensearches=s['open'], foundfiles=s['found'], sumsearches=s['sum'], running=s['running'])}/>"

    @staticmethod
    def entry_xml(e):
        names = "".join(f"<filename {attrs(name=n, user=c)}/>\n" for n, c in e["names"])
        return f"<searchentry {attrs(id=e['id'], searchid=e['searchid'], checksum=e['checksum'], size=e['size'])}>\n{names}</searchentry>"

    def search_xml(self):
        return "\n".join([self.search_row(s) for s in self.searches.values()] +
                         [self.entry_xml(e) for e in self.entries.values()])

    def find_object(self, oid: int):
        """Return the XML of any object by id, or None."""
        if oid in self.shares:
            return self.share_row(self.shares[oid])
        for coll, fn in ((self.downloads, self.download_xml), (self.users, self.user_xml),
                         (self.uploads, self.upload_xml), (self.servers, self.server_row),
                         (self.searches, self.search_row), (self.entries, self.entry_xml)):
            if oid in coll:
                return fn(coll[oid])
        if oid == self.information_id:
            return self.information_row()
        if oid == self.networkinfo_id:
            return self.networkinfo_row()
        return None

    def new_session(self, ip: str) -> int:
        while True:
            sid = random.randint(1, 2**31 - 1)
            if sid not in self.sessions:
                break
        self.sessions[sid] = {"ip": ip, "last": time.time(), "removed": []}
        return sid

    def validate_session(self, sid: int, ip: str):
        """Reject unknown, expired or foreign-IP sessions; a valid access keeps the session alive."""
        session = self.sessions.get(sid)
        if session is None or session["ip"] != ip:
            return None
        session["last"] = time.time()
        return session

    def modified_xml(self, filters: set[str] | None, timestamp: int = 0, session: dict | None = None):
        """filters None = no filter parameter (everything); a set restricts the categories."""
        full = filters is None
        filters = filters or set()
        parts = [f"<time>{now_ms()}</time>"]
        if full or "ids" in filters:
            if session is None:
                parts.append(self.ids_xml())
            else:
                removed = "".join(f'<object id="{i}"/>\n' for i in reversed(session["removed"]))
                session["removed"] = []  # Reading the list clears it.
                parts.append(f"<removed>\n{removed}</removed>")
        wanted = {"user": "user", "upload": "uploads", "download": "down", "server": "server",
                  "search": "search", "searchentry": "search", "information": "informations",
                  "networkinfo": "informations"}
        for i, (kind, xml) in sorted(self.snapshot.items()):
            if kind == "share" or not (full or wanted[kind] in filters):
                continue
            if self.stamps.get(i, 0) >= timestamp:
                parts.append(xml)
        return "<applejuice>\n" + "\n".join(p for p in parts if p) + "\n</applejuice>"

    def settings_xml(self):
        s = self.settings
        body = "".join(f"<{k}>{escape(str(v))}</{k}>" for k, v in s.items())
        shares = "".join(f'<directory name={quoteattr(n)} sharemode={quoteattr(m)}/>' for n, m in self.share_dirs)
        return f"<settings>{body}<share>{shares}</share></settings>"

    @staticmethod
    def share_row(s):
        return f"<share {attrs(id=s['id'], filename=s['filename'], shortfilename=s['short'], size=s['size'], checksum=s['checksum'], priority=s['priority'], lastasked=s['lastasked'], askcount=s['askcount'], searchcount=s['searchcount'])}/>"

    def share_xml(self):
        rows = "\n".join(self.share_row(s) for s in self.shares.values())
        return f"<applejuice>\n<shares>\n{rows}\n</shares>\n</applejuice>"

    @staticmethod
    def parts_xml(size: int, parts: list[tuple[int, int]]):
        """Gap-free availability map: -1 local, 0 unavailable, n > 0 known sources. Equal neighbours are merged."""
        merged: list[tuple[int, int]] = []
        for start, kind in sorted(parts):
            if not merged or merged[-1][1] != kind:
                merged.append((start, kind))
        rows = "\n".join(f'<part fromposition="{f}" type="{t}"/>' for f, t in merged)
        return f'<applejuice>\n<fileinformation filesize="{size}"/>\n{rows}\n</applejuice>'

    def download_partlist_xml(self, d):
        size, ready = d["size"], d["ready"]
        parts = []
        if ready > 0:
            parts.append((0, -1))
        if ready < size or not parts:
            parts.append((ready, len(d["users"])))
        return self.parts_xml(size, parts)

    def user_partlist_xml(self, u):
        d = self.downloads[u["downloadid"]]
        if u["status"] == 1:  # Nothing known about an unqueried source yet.
            return self.parts_xml(d["size"], [(0, 0)])
        return self.parts_xml(d["size"], [(0, -1), (d["size"] // 2, 0)])

    def directory_xml(self, directory: str | None):
        extra = sorted({"archive", "catalog", "incoming", "isos", "temp"})
        tree = {"/": ["mock", "home", "tmp"], "/mock": extra, "/home": ["user"]}
        # Without a parameter the Unix Core lists the file system roots
        key = None if directory is None else (directory.rstrip("/") or "/")
        if key is None:
            return '<applejuice>\n<filesystem seperator="/"/><dir name="/" isfilesystem="true" type="4"/></applejuice>'
        prefix = self.ISO_ROOT + "/"
        if key == self.ISO_ROOT or key.startswith(prefix):
            depth = key[len(prefix):].count("/") + 1 if key != self.ISO_ROOT else 0
            children = set()
            for share in self.shares.values():
                if share["filename"].startswith(prefix):
                    parts = share["filename"][len(prefix):].split("/")[:-1]
                    if key == self.ISO_ROOT and parts:
                        children.add(parts[0])
                    elif key != self.ISO_ROOT and parts[:depth] == key[len(prefix):].split("/") and len(parts) > depth:
                        children.add(parts[depth])
            tree[key] = sorted(children)
        # Subfolders carry type 4 and no path, as the Unix listing does; clients derive the path.
        dirs = "".join(f'<dir name={quoteattr(n)} isfilesystem="true" type="4"/>' for n in tree.get(key, []))
        return f'<applejuice>\n<filesystem seperator="/"/>{dirs}</applejuice>'

    # ---- Actions --------------------------------------------------------
    def ids_param(self, q) -> list[int]:
        """`id`, then id1, id2, ... up to the first missing number; unparsable values are skipped."""
        ids = []
        n = 0
        while True:
            key = "id" if n == 0 else f"id{n}"
            if key not in q:
                if n == 0:
                    n = 1
                    continue
                break
            try:
                ids.append(parse_int(q[key][0]))
            except ValueError:
                pass
            n += 1
        return ids

    def single_id(self, q) -> int:
        try:
            return parse_int(q["id"][0]) if "id" in q else -1
        except ValueError:
            return -1

    def download_of(self, oid):
        return self.downloads.get(oid) if oid > 0 else None

    def action_ctype(self, name: str, q: dict) -> str:
        """Handlers without a Content-Type answer text/html, all others text/xml."""
        if name in HTML_ACTIONS or (name == "setpassword" and "newpassword" not in q):
            return "text/html"
        return "text/xml"

    def find_share(self, checksum: str, size: int):
        for share in self.shares.values():
            if share["checksum"] == checksum and share["size"] == size:
                return share
        return None

    def attach_part_share(self, d):
        """Reuse an existing share with the same hash and size, else create a part-file share."""
        existing = self.find_share(d["hash"], d["size"])
        if existing is not None:
            d["shareid"] = existing["id"]
            return
        sid = self.new_id()
        self.shares[sid] = {"id": sid, "filename": f"{self.settings['temporarydirectory']}{d['temporaryfilenumber']}.data",
                            "short": d["filename"], "size": d["size"], "checksum": d["hash"], "priority": 1,
                            "lastasked": 0, "askcount": 0, "searchcount": 0}
        d["shareid"] = sid

    def action(self, name: str, q: dict) -> str:
        q = {k.lower(): v for k, v in q.items()}
        ids = self.ids_param(q)
        if name in ("pausedownload", "resumedownload", "canceldownload"):
            out = []
            for i in ids:
                d = self.download_of(i)
                if d is None:
                    out.append("error: invalid id")
                    continue
                if name == "pausedownload" and d["status"] == 0:
                    d["status"] = 18
                elif name == "resumedownload" and d["status"] == 18:
                    d["status"] = 0
                elif name == "canceldownload":
                    if d["status"] == 0:
                        d["status"] = 18  # cancelDownload pauses first
                    if d["status"] in (18, 16, 1):
                        d["status"] = 15
                        d["cancel_at"] = time.time() + CANCEL_DELAY
                out.append("ok")
            return "".join(out)
        if name == "cleandownloadlist":
            for i in [i for i, d in self.downloads.items() if d["status"] in (13, 14, 17)]:
                for u in self.downloads[i]["users"]:
                    self.users.pop(u, None)
                self.drop_part_share(self.downloads[i])
                del self.downloads[i]
            return "ok"
        if name == "renamedownload":
            if "name" not in q:
                return "error: missing name"
            d = self.download_of(self.single_id(q))
            if d is None:
                return "error: invalid id"
            d["filename"] = q["name"][0]
            for u in d["users"]:
                self.users[u]["filename"] = d["filename"]
            return "ok"
        if name == "settargetdir":
            target = q.get("dir", [None])[0]
            if target is None:
                return "error: missing dir"
            if ".." in target or ":" in target:
                return "error: invalid dir"
            d = self.download_of(self.single_id(q))
            if d is None:
                return "error: invalid id"
            d["targetdirectory"] = target
            return "ok"
        if name == "setpowerdownload":
            try:
                level = parse_int(q.get("powerdownload", ["0"])[0])
            except ValueError:
                level = 0
            results = []
            for i in ids:
                d = self.download_of(i)
                if d is None:
                    results.append("error: invalid id")
                    continue
                if level == 0 or 12 <= level <= 490:  # other values are ignored
                    d["powerdownload"] = level
                results.append(f"ok: set to {d['powerdownload']}")
            return "".join(results)
        if name == "setpriority":
            try:
                priority = parse_int(q.get("priority", ["0"])[0])
            except ValueError:
                priority = 0
            results = []
            for i in ids:
                share = self.shares.get(i)
                if share is None:
                    results.append("error: invalid id")
                    continue
                if 1 <= priority <= 250:  # other values are ignored
                    others = sum(o["priority"] for o in self.shares.values() if o is not share and o["priority"] > 1)
                    share["priority"] = max(1, 1000 - others) if others + priority > 1000 else priority
                results.append(f"ok: set to {share['priority']}")
            return "".join(results)
        if name == "search":
            text = q.get("search", [""])[0]
            if text == "":
                return "failure"
            self.add_search(text, running=True)
            return ""
        if name == "cancelsearch":
            search = self.searches.get(self.single_id(q))
            if search is None:
                return "error: invalid id"
            search["running"] = "false"
            search["open"] = 0
            return "ok"
        if name == "serverlogin":
            if "id" not in q:
                return "failure"
            try:
                oid = parse_int(q["id"][0])
            except ValueError as exc:
                return f"failure: {exc}"
            if oid in self.servers:
                self.connected_server = -1
                self.connecting = oid
                self.connecting_since = time.time()
                self.servers[oid]["connectiontry"] += 1
            elif oid in self.snapshot or oid in self.shares:
                return "failure: not a server"
            return "ok"
        if name == "removeserver":
            oid = self.single_id(q)
            if oid not in self.servers:
                return "error: invalid id"
            if oid not in (self.connecting, self.connected_server):  # The active server stays despite ok.
                del self.servers[oid]
            return "ok"
        if name == "processlink":
            return self.process_link(q.get("link", [None])[0], q.get("subdir", [None])[0])
        if name == "setsettings":
            return self.set_settings(q)
        if name == "setpassword":
            new = q.get("newpassword", [None])[0]
            if new is None:
                return "error: unknown password"
            if len(new) != 32:
                return "error: invalid md5checksum"
            try:
                bytes(parse_int(new[i:i + 2], radix=16) for i in range(0, 32, 2))
            except ValueError as exc:
                return f"error: {exc}"
            self.password_md5 = new.lower()  # The running request was already authenticated with the old hash.
            return "ok"
        if name in ("sharecheck", "stopsharecheck"):
            return "ok"
        if name == "exitcore":
            self.exited = True
            return "ok"
        raise Redirect("/help")

    def set_settings(self, q: dict) -> str:
        """Apply field by field, so a parse error keeps earlier changes."""
        s = self.settings

        def number(key):
            try:
                return parse_int(q[key][0])
            except ValueError:
                raise AbortConnection(key) from None

        def per_slot():
            m = s["maxupload"] // 1024
            s["speedperslot"] = min(round_half_up(m ** 0.6), max(round_half_up(m ** 0.2), s["speedperslot"]))

        if q.get("nickname", [""])[0] != "":
            s["nick"] = q["nickname"][0]
        for key in ("port", "xmlport"):
            if key in q:
                s[key] = number(key)
        if "autoconnect" in q:
            s["autoconnect"] = "true" if q["autoconnect"][0].lower() == "true" else "false"
        if "maxupload" in q:
            s["maxupload"] = max(3072, number("maxupload"))
            per_slot()
        if "maxdownload" in q:
            s["maxdownload"] = max(0, number("maxdownload"))
        if "maxconnections" in q:
            s["maxconnections"] = max(30, number("maxconnections"))
        if "maxsourcesperfile" in q:
            s["maxsourcesperfile"] = number("maxsourcesperfile")
        if "maxnewconnectionsperturn" in q:
            s["maxnewconnectionsperturn"] = min(max(1, number("maxnewconnectionsperturn")), 200)
        if "speedperslot" in q:
            s["speedperslot"] = number("speedperslot")
            per_slot()
        if "incomingdirectory" in q:
            directory = q["incomingdirectory"][0]
            checked = directory if directory.endswith("/") else directory + "/"
            if checked != s["incomingdirectory"]:
                s["incomingdirectory"] = checked
                self.set_share_dir(directory.rstrip("/") or "/", "subdirectory")
        if "temporarydirectory" in q and not self.downloads:
            directory = q["temporarydirectory"][0]
            s["temporarydirectory"] = directory if directory.endswith("/") else directory + "/"
        if "countshares" in q:
            # Core clears the list, adds the incoming directory (sharemode 0), then walks N..1;
            # entries lacking directory or sharesub are skipped, an existing path only changes its mode.
            count = number("countshares")
            self.share_dirs = []
            self.set_share_dir(s["incomingdirectory"].rstrip("/") or "/", "subdirectory")
            for n in range(count, 0, -1):
                path, sub = q.get(f"sharedirectory{n}", [None])[0], q.get(f"sharesub{n}", [None])[0]
                if path is not None and sub is not None:
                    self.set_share_dir(path.rstrip("/") or "/", "subdirectory" if sub.lower() == "true" else "singledirectory")
        return "ok"

    def set_share_dir(self, path: str, mode: str) -> None:
        for index, (existing, _mode) in enumerate(self.share_dirs):
            if existing == path:
                self.share_dirs[index] = (path, mode)
                return
        self.share_dirs.append((path, mode))

    def process_link(self, link: str | None, subdir: str | None = None) -> str:
        if link is None:
            return "failure: link missing"
        if link.endswith("/"):  # exactly one trailing slash is stripped
            link = link[:-1]
        if not link.startswith("ajfsp://"):
            return ""
        parts = link[len("ajfsp://"):].split("|")

        def part(index):
            if index >= len(parts):
                raise IndexError(f"Index {index} out of bounds for length {len(parts)}")
            return parts[index]

        try:
            if part(0) == "file":
                filename, checksum, size_text = part(1), part(2), part(3)
                try:
                    size = parse_int(size_text, 64)
                    if len(checksum) != 32:
                        raise ValueError(checksum)
                    bytes.fromhex(checksum)
                except ValueError:
                    return "failure: incorrect link"
                checksum = checksum.lower()
                existing = next((d for d in self.downloads.values() if d["hash"] == checksum and d["size"] == size), None)
                if existing is None and self.find_share(checksum, size) is not None:
                    return "already downloaded"
                if existing is None:
                    did = self.add_download(filename, size, md5=checksum)
                    existing = self.downloads[did]
                    for source in parts[4:]:
                        fields = source.split(":")
                        if len(fields) in (2, 4):
                            uid = self.add_source(did, 1, 0, "", source=1)
                            self.users[uid].update(version="0.0.0.0", os=0, directstate=0)
                if subdir is not None and ".." not in subdir and ":" not in subdir:
                    existing["targetdirectory"] = subdir  # values with "..", ":" are silently ignored
                return "ok"
            if part(0) == "server":
                host, port_text = part(1), part(2)
                try:
                    port = parse_int(port_text)
                except ValueError:
                    return "failure: incorrect link"
                known = next((x for x in self.servers.values() if x["host"] == host and x["port"] == port), None)
                if known is None:
                    self.add_server("", host, port)
                    known = self.servers[max(self.servers)]
                known["lastseen"] = now_ms()
                return ""
            return "failure: incorrect link"
        except IndexError as exc:
            return f"failure: invalid link :: {exc}"


class Handler(BaseHTTPRequestHandler):
    server_version = "ajcore-mock"
    state: State

    def version_string(self):
        return self.server_version

    def log_message(self, format, *args):
        if self.server.verbose:  # type: ignore[attr-defined]
            super().log_message(format, *args)

    def send(self, code: int, body: str, ctype: str | None = "text/xml", headers=None, compress: bool = False):
        data = body.encode("utf-8", "replace")
        if ctype == "text/html" and not data and code == 200:
            data = b"<html><body></body></html>"  # empty text/html answers get a default body
        if compress:
            # mode=zip is zlib. A handler-set Content-Type becomes application/zlib, otherwise the HTTP layer
            # falls back to text/html; OPTIONS stays text/plain.
            data = zlib.compress(data)
            ctype = "application/zlib" if ctype == "text/xml" else ctype  # text/html is the HTTP layer default
            full_type = ctype
        else:
            full_type = f"{ctype}; charset=UTF-8" if ctype else None
        self.send_response(code)
        if full_type:
            self.send_header("Content-Type", full_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):
        url = urllib.parse.urlsplit(self.path)
        q = urllib.parse.parse_qs(url.query, keep_blank_values=True)
        zipped = q.get("mode", [""])[-1].lower() == "zip"
        self.send(200, "", "text/plain", compress=zipped)

    def do_GET(self):
        self.handle_request({})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length).decode("utf-8", "replace")
        # Only an exact application/x-www-form-urlencoded type (no charset) is parsed as form.
        form = {}
        if self.headers.get("Content-Type", "").lower() == "application/x-www-form-urlencoded":
            form = urllib.parse.parse_qs(raw, keep_blank_values=True)
        self.handle_request(form)

    HELP = ('<html>\n<body>\n<h2>Help</h2>\n<p>You will find more information about this informationserver at '
            '<a href="http://www.applejuicenet.cc/">http://www.applejuicenet.cc</a></p>\n</body>\n</html>\n')

    def handle_request(self, form):
        url = urllib.parse.urlsplit(self.path)
        raw = urllib.parse.parse_qs(url.query, keep_blank_values=True)
        # Parameter names are lower-cased, the last value wins and form values override the query.
        q = {k.lower(): [v[-1]] for k, v in raw.items()}
        q.update({k.lower(): [v[-1]] for k, v in form.items()})
        zipped = q.get("mode", [""])[0].lower() == "zip"
        segments = [x for x in url.path.split("/") if x]
        first = segments[0].lower() if segments else ""
        if first == "help":
            return self.send(200, self.HELP, "text/html", compress=zipped)
        if first in ("wrongpassword", "wrongsession"):
            what = "password" if first == "wrongpassword" else "session"
            body = (f'<html>\n<body>\n<p>wrong {what}. access denied. for more information look '
                    f'<a href="/help">help</a>.</p>\n</body>\n</html>\n')
            return self.send(200, body, "text/html", compress=zipped)
        st = self.state
        with st.lock:
            password = self.headers.get("X-AppleJuice-Password")  # A present header wins, even when empty.
            if password is None:
                password = q.get("password", [None])[0]
            if password is None or password.lower() != st.password_md5:
                return self.send(302, "", None, {"Location": "/wrongpassword"}, zipped)
            st.tick()
            try:
                body, ctype = self.route(segments, q)
            except Redirect as redirect:
                return self.send(302, "", None, {"Location": redirect.location}, zipped)
            except AbortConnection:
                self.close_connection = True
                return
            st.sync_objects()
            self.send(200, body, ctype, compress=zipped)
            if st.exited:
                threading.Thread(target=self.server.shutdown, daemon=True).start()

    def route(self, segments: list[str], q: dict):
        st = self.state
        head = '<?xml version="1.0"?>\n'
        lowered = [x.lower() for x in segments]
        if len(lowered) >= 2 and lowered[0] == "function":
            return st.action(lowered[1], q), st.action_ctype(lowered[1], q)
        if len(lowered) < 2 or lowered[0] != "xml":
            raise Redirect("/help")
        name = lowered[1]
        if name == "information.xml":
            return head + '<applejuice><generalinformation><version>0.35.185.93</version><filesystem seperator="/"/><system>Linux</system></generalinformation></applejuice>', "text/xml"
        if name == "getsession.xml":
            sid = st.new_session(self.client_address[0])
            return head + f'<applejuice>\n<session id="{sid}"/>\n</applejuice>', "text/xml"
        if name == "settings.xml":
            return head + st.settings_xml(), "text/xml"
        if name == "share.xml":
            return head + st.share_xml(), "text/xml"
        if name == "directory.xml":
            return head + st.directory_xml(q.get("directory", [None])[0]), "text/xml"
        if name == "modified.xml":
            filters = None
            if "filter" in q:
                filters = {f.strip().lower() for f in q["filter"][0].split(";") if f.strip()}
            try:
                timestamp = parse_int(q["timestamp"][0], 64) if "timestamp" in q else 0
            except ValueError:
                timestamp = 0
            session = None
            if "session" in q:
                try:
                    sid = parse_int(q["session"][0])
                except ValueError:
                    sid = None
                if sid is not None:
                    session = st.validate_session(sid, self.client_address[0])
                    if session is None:
                        raise Redirect("/wrongsession")
            return head + st.modified_xml(filters, timestamp, session), "text/xml"
        if name in ("downloadpartlist.xml", "userpartlist.xml"):
            oid = st.single_id(q)
            wanted = st.downloads if name == "downloadpartlist.xml" else st.users
            obj = wanted.get(oid)
            if obj is None:
                if oid in st.snapshot or oid in st.shares:
                    return "failure: wrong object type", "text/html"
                return "failure: invalid id", "text/html"
            body = st.download_partlist_xml(obj) if wanted is st.downloads else st.user_partlist_xml(obj)
            return head + body, "text/xml"
        if name == "getobject.xml":
            oid = st.single_id(q)
            xml = st.find_object(oid) if oid > 0 else None
            if xml is None:
                return "error: invalid id", "text/xml"
            return head + f"<applejuice>{xml}</applejuice>", "text/xml"
        raise Redirect("/help")


def run(host: str, port: int, scenario: str, password: str | None, verbose: bool, shareidx_bytes: int = DEFAULT_INDEX_BYTES, shareidx_output: Path | None = None, iso_count: int = 300):
    """Start a synthetic HTTP/XML Core service for any API client."""
    # Fixtures never connect to external servers.
    md5 = EMPTY_PASSWORD_MD5 if password is None else hashlib.md5(password.encode()).hexdigest()
    state = State(scenario, md5)
    state.settings['xmlport'] = port
    index = populate(state, shareidx_bytes) if shareidx_bytes else None
    if index is not None and shareidx_output is not None:
        write(shareidx_output, index)
    # ISOs are added after the index fixture: real ISO sizes would need tens of MB of subhashes.
    if scenario != 'empty':
        state.add_iso_catalog(iso_count)
        if scenario == 'busy':
            state.add_big_directory()
    print(json.dumps({'shareidx_bytes': len(index) if index else 0, 'shares': len(state.shares), 'share_api_bytes': len(state.share_xml().encode())}), flush=True)
    Handler.state = state
    del index  # Release the generated disk-index fixture after startup.
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.verbose = verbose  # type: ignore[attr-defined]
    print(json.dumps({"mock_core": f"http://{host}:{port}", "scenario": scenario, "password_md5": md5}), flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--scenario", default="busy", choices=["empty", "busy", "firewalled", "disconnected"])
    p.add_argument("--password", default=None, help="Plain text; default: empty")
    p.add_argument("--shareidx-bytes", type=int, default=DEFAULT_INDEX_BYTES, help="Synthetic index size in bytes; 0 disables additional shares")
    p.add_argument("--iso-count", type=int, default=300, help="Number of ISO files shared below /mock/isos in folders per distribution and release; 0 disables")
    p.add_argument("--shareidx-output", type=Path, help="Optional generated shareidx.xml output path")
    p.add_argument("-v", "--verbose", action="store_true")
    # The default bind stays loopback-only.
    a = p.parse_args()
    run(a.host, a.port, a.scenario, a.password, a.verbose, a.shareidx_bytes, a.shareidx_output, a.iso_count)
