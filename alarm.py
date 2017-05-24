#!/usr/bin/python3
# coding: utf-8

import json
import hashlib
import gzip
import http.client as httpc
import os
import io
import shutil
import signal
import struct
import sys
import urllib.parse
import textwrap
import traceback
import time
import zlib

ALARM_VERSION = '0.1'
ALARMFILE_MAGIC = b'0\x9e\xb9\x08'
ALARM_INDEX_NAME = 'alarm.idx'
GITHUB_API_BASE = 'api.github.com'
GITHUB_MAX_PAGES = 10

# If the user requests an abortion of the operation, this flag gets set
global_stop_flag = False

global_api_token = None

def request_stop_handler(signal, frame):
    global global_stop_flag

    if global_stop_flag:
        sys.exit(5)
    else:
        global_stop_flag = True
        print('Caught interrupt, waiting for current operation to finish (press again to exit immediately)')

class limit:
    core_left    = 0
    core_reset   = 0
    search_left  = 0
    search_reset = 0

def has_api_left(num_core, num_search):
    t = time.time()
    co = limit.core_left   >= num_core   or limit.core_reset   < t
    se = limit.search_left >= num_search or limit.search_reset < t
    return co and se

def init_github_api():
    global global_api_token
    with open(options.token_file, 'r') as f:
        global_api_token = f.read().strip()
    
    conn = httpc.HTTPSConnection(GITHUB_API_BASE)
    try:
        data = get_from_api(conn, '/rate_limit')
    finally:
        conn.close()
    
    limit.core_left    = data['resources']['core']['remaining']
    limit.core_reset   = data['resources']['core']['reset']
    limit.search_left  = data['resources']['search']['remaining']
    limit.search_reset = data['resources']['search']['reset']

def get_from_api(conn, url):
    
    h = {'User-Agent': options.user_agent, 'Accept': 'application/vnd.github.v3+json',
         'Authorization': 'token ' + global_api_token }

    is_core_req = not url.startswith('/search')

    left  = limit.core_left  if is_core_req else limit.search_left
    reset = limit.core_reset if is_core_req else limit.search_reset
    dur = reset - time.time()
    
    if left == 0 and dur > 0:
        print('No api requests remaining, sleeping for %.0fs' % dur)
        time.sleep(dur)
    
    conn.request('GET', url, headers=h)
    r = conn.getresponse()

    if is_core_req:
        limit.core_left    = int(r.getheader('X-RateLimit-Remaining'))
        limit.core_reset   = int(r.getheader('X-RateLimit-Reset')) + 2
    else:
        limit.search_left  = int(r.getheader('X-RateLimit-Remaining'))
        limit.search_reset = int(r.getheader('X-RateLimit-Reset')) + 2
    
    return json.loads(r.read().decode('utf-8'))

def get_some_files_hide_errors(owner, repo):
    try:
        return get_some_files(owner, repo)
    except:
        print('Error.')
        traceback.print_exc(file=sys.stderr)
        return []

def get_some_files(owner, repo):
    MAX_BRANCHES = options.files_max_refs

    if not has_api_left(1 + 2*MAX_BRANCHES, 0):
        print('Downloading tree information skipped, no api limit left')
        return []
    
    print('Downloading tree information... ', end='')
    sys.stdout.flush()
    
    conn = httpc.HTTPSConnection(GITHUB_API_BASE)
    try:
        data = get_from_api(conn, '/repos/%s/%s/git/refs' % (owner, repo))
        commits = {i['object']['sha'] for i in data[:MAX_BRANCHES]}
        loc = '/repos/%s/%s/git/commits/%s'
        trees = {get_from_api(conn, loc % (owner, repo, i))['tree']['sha'] for i in commits}

        files = set()
        for t in trees:
            data = get_from_api(conn, '/repos/%s/%s/git/trees/%s?recursive=1' % (owner, repo, t))
            files.update((-j['size'], j['sha']) for j in data['tree'] if j['type'] == 'blob')

        conn.close()

        # Biggest files first
        files = list(files)
        files.sort()
        files = [sha for _, sha in files]

        print('Done.')
        print('Found %d files' % len(files))

        return files
    finally:
        conn.close()

def get_top100_for_language(lang):
    print('Querying top100 repositories for %s... ' % lang, end='')
    sys.stdout.flush()
    
    conn = httpc.HTTPSConnection(GITHUB_API_BASE)
    try:
        params = urllib.parse.urlencode({'q': 'language:"%s"' % lang, 'sort': 'stars', 'per_page': 100})
        data = get_from_api(conn, '/search/repositories?%s' % params)

        print('Done.')

        return [(i['owner']['login'], i['name']) for i in data['items']]
    finally:
        conn.close()

global_sector_max_stars = {}

def get_small_repos(page):
    # page starts with one, due to GitHub API sillyness
    print('Querying small repositories, page %d... ' % page, end='')
    sys.stdout.flush()

    conn = httpc.HTTPSConnection(GITHUB_API_BASE)
    try:
        sector = (page - 1) // GITHUB_MAX_PAGES
        page = page - sector * GITHUB_MAX_PAGES
        data = get_small_repos_helper(sector, page, conn)
        print('Done.')
        return data
    finally:
        conn.close()

def get_small_repos_helper(sector, page, conn):
    if sector == 0:
        q_suf = ''
    else:
        if sector not in global_sector_max_stars:
            get_small_repos_helper(sector - 1, GITHUB_MAX_PAGES, conn)
        max_stars = global_sector_max_stars[sector]
        q_suf = f' stars:<={max_stars}'
        
    mm = options.small_min, options.small_max, q_suf
    params = urllib.parse.urlencode({
        'q': 'size:%d..%d%s' % mm, 'sort': 'stars', 'per_page': 100, 'page': page
    })
    data = get_from_api(conn, '/search/repositories?%s' % params)

    if page == GITHUB_MAX_PAGES:
        global_sector_max_stars[sector+1] = int(data['items'][-1]['stargazers_count'])

    return [(i['owner']['login'], i['name']) for i in data['items']]
            
def pkt_line(data):
    i = 0
    while i < len(data):
        l = int(data[i:i+4], 16)
        if l == 0:
            yield None
            i += 4
        else:
            yield data[i+4:i+l]
            i += l

def mk_pkt_line(lst):
    data = b''
    for i in lst:
        if i is None:
            data += b'0000'
        else:
            data += b'%04x' % (len(i) + 4)
            data += i
    return data

def shorten(s, maxlen=80):
    return s if len(s) <= maxlen else s[:maxlen-3] + b'...'


class ObjType:
    OBJ_BAD = -1
    OBJ_NONE = 0
    OBJ_COMMIT = 1
    OBJ_TREE = 2
    OBJ_BLOB = 3
    OBJ_TAG = 4
    OBJ_OFS_DELTA = 6
    OBJ_REF_DELTA = 7

    @classmethod
    def typename(cls, i):
        if   i == cls.OBJ_NONE:   return None
        elif i == cls.OBJ_COMMIT: return b'commit'
        elif i == cls.OBJ_TREE:   return b'tree'
        elif i == cls.OBJ_BLOB:   return b'blob'
        elif i == cls.OBJ_TAG:    return b'tag'
        else: assert False

HASH_DETAIL = 6

class Tree:
    typ = ObjType.OBJ_TREE
    
    @classmethod
    def parse(cls, b):
        self = cls()
        self.blob = b
        self.entries = []
        i = j = 0
        while i < len(b):
            i, j = b.find(b' ', i+1), i
            assert i != -1
            mode = b[j:i]
            i, j = b.find(b'\0', i+1), i+1
            name = b[j:i]
            sha = b[i+1:i+21].hex().encode('ascii')
            self.entries.append((mode, name, sha))
            i += 21
        return self
            
    def __str__(self):
        return (b'Tree(entries=[\n  %s\n])' % b',\n  '.join(b'(%s, %s, %s)' % 
            (i[0], i[2][:HASH_DETAIL], i[1]) for i in self.entries)).decode('utf-8')

class Commit:
    typ = ObjType.OBJ_COMMIT
    
    @classmethod
    def parse(cls, b):
        self = cls()
        self.blob = b
        it = iter(b.splitlines())
        cmd, sha = next(it).split(b' ')
        assert cmd == b'tree'
        self.tree = sha
        self.parents = []
        while True:
            cmd, sha = next(it).split(b' ', maxsplit=1)
            if cmd != b'parent': break
            self.parents.append(sha)
        # Ignore the rest of the data
        return self

    def __str__(self):
        return (b'Commit(tree=%s, parents=[%s])' % (self.tree[:HASH_DETAIL], b', '.join(
            i[:HASH_DETAIL] for i in self.parents))).decode('utf-8')

class Blob:
    @classmethod
    def parse(cls, b):
        self = cls()
        self.blob = b
        return self
        
def get_typ(i):
    return ['OBJ_NONE', 'OBJ_COMMIT', 'OBJ_TREE', 'OBJ_BLOB',
            'OBJ_TAG', None, 'OBJ_OFS_DELTA', 'OBJ_REF_DELTA'][i]

def objhead(b, off = 0):
    typ = (b[off] >> 4) & 7
    size = b[off] & 15
    i = 0
    while b[off + i] & 128:
        i += 1
        size |= (b[off + i] & 127) << (i*7 - 3)
    return typ, size, off+i+1


# translation of patch-delta.c:patch_delta
def patch_delta(src, delta):
    def varint(buf, start):
        # These varints are different to the ofs_delta ones
        i = x = 0
        while True:
            x |= (buf[start+i] & 127) << 7*i
            i += 1
            if buf[start+i-1] & 128 == 0: break
        return start+i, x

    data = 0

    data, size = varint(delta, data)
    assert size == len(src)

    data, size = varint(delta, data)
    dst = bytearray(size)

    out = 0;
    while data < len(delta):
        cmd = delta[data]; data += 1
        
        if cmd & 0x80:
            cp_off = cp_size = 0;
            if cmd & 0x01: cp_off   = delta[data];       data += 1
            if cmd & 0x02: cp_off  |= delta[data] << 8;  data += 1
            if cmd & 0x04: cp_off  |= delta[data] << 16; data += 1
            if cmd & 0x08: cp_off  |= delta[data] << 24; data += 1
            if cmd & 0x10: cp_size  = delta[data];       data += 1
            if cmd & 0x20: cp_size |= delta[data] << 8;  data += 1
            if cmd & 0x40: cp_size |= delta[data] << 16; data += 1
            
            if cp_size == 0: cp_size = 0x10000
            
            if cp_size > 2**31-1 - cp_off or cp_off + cp_size > len(src) or cp_size > size:
                assert False
            dst[out:out+cp_size] = src[cp_off:cp_off+cp_size]
            out += cp_size
            size -= cp_size
        elif cmd:
            assert cmd <= size
            dst[out:out+cmd] = delta[data:data+cmd]
            out += cmd
            data += cmd
            size -= cmd
        else:
            assert False

    assert data == len(delta) and size == 0
    
    return dst

class Side_band_64k:
    def __init__(self, fd):
        self.fd = fd
        self.left = 0

    def readinto(self, buf):
        m = memoryview(buf)

        m1, m2 = m[:self.left], m[self.left:]
        num = self.fd.readinto(m1)
        assert num == len(m1)
        self.left -= len(m1)
        if not m2: return num
        assert not self.left
        
        while True:
            b = self.fd.read(4)
            if not b: break
            size = int(b, 16)
            if not size: break
            stream = self.fd.read(1)[0]
            size -= 5
            if stream == 1:
                m1, m2 = m2[:size], m2[size:]
                assert self.fd.readinto(m1) == len(m1)
                num += len(m1)
                self.left += size - len(m1)
                if not m2: return num
                assert not self.left
            elif stream == 2:
                self.fd.read(size)
        
        return num

    def read(self, num):
        buf = bytearray(num)
        num = self.readinto(buf)
        return buf[:num]

    def close(self):
        self.fd.close()
    
global_64k_buffer = bytearray(64*1024)

MAX_HEADER_SIZE = 256
    
def objs(f, do_parse=True):
    buf = memoryview(global_64k_buffer)
    class num: pass

    num.skipped = 0
    num.commits = 0
    num.trees   = 0
    num.rbytes  = 0

    time_last = time.clock()

    blobstore = {}
    typestore = {}
    offsstore = {}

    num.rbytes += f.readinto(buf[:12])
    assert buf[:8] == b'PACK\0\0\0\2'
    
    num.total  = int.from_bytes(buf[8:12], byteorder='big')
    num.left   = num.total

    # Compatibility with our own metadata stream
    if num.left == 0:
        num.left = None

    if do_parse:
        cls_commit = Commit
        cls_tree   = Tree
    else:
        cls_commit = cls_tree = Blob

    def varint(start):
        # These varints are different to the patch_delta ones
        i = 0
        x = buf[start] & 127
        while buf[start+i] & 128:
            i += 1
            x = ((x + 1) << 7) + (buf[start+i] & 127)
        return start + i + 1, x
    
    def skip(start, end, offset):
        while True:
            o.decompress(buf[start:end])
            if o.eof: break
            start = 0
            end = f.readinto(buf)
            num.rbytes += end
            assert end
        start = end - len(o.unused_data)
        num.skipped += 1
        offsstore[offset] = None
        return start, end
    
    def read(start, end):
        data = bytearray()
        while True:
            data += o.decompress(buf[start:end])
            if o.eof: break
            start = 0
            end = f.readinto(buf)
            num.rbytes += end
            assert end
        start = end - len(o.unused_data)
        return start, end, data

    def handle(typ, data, offset):
        # see sha1_file.c:write_sha1_file_prepare
        h = hashlib.sha1()
        h.update(b'%s %d\0' % (ObjType.typename(typ), len(data)))
        h.update(data)
        sha = h.digest().hex()
        blobstore[sha] = data
        typestore[sha] = typ
        offsstore[offset] = sha
        if typ == ObjType.OBJ_COMMIT:
            num.commits += 1
            return sha, cls_commit.parse(data)
        elif typ == ObjType.OBJ_TREE:
            num.trees += 1
            return sha, cls_tree.parse(data)
        else:
            assert False

    start = 0
    end = f.readinto(buf)
    num.rbytes += end
    while num.left:
        if start == 0 and time.clock() > time_last + 1:
            time_last = time.clock()
            if num.left is not None:
                print('Downloading... (%d/%d)' % (num.total - num.left, num.total))
            else:
                print('Reading... (%d)' % (num.total - num.left, num.total))
                
        if start == end: break
        offset = num.rbytes - (end - start)
        typ, size, off = objhead(buf[start:])

        # Compatibility with our own metadata stream
        if typ == ObjType.OBJ_NONE:
            break
        
        start += off
        o = zlib.decompressobj()
        if typ in (ObjType.OBJ_COMMIT, ObjType.OBJ_TREE):
            start, end, data = read(start, end)
            assert len(data) == size
            yield handle(typ, data, offset)
        elif typ in (ObjType.OBJ_BLOB, ObjType.OBJ_TAG):
            start, end = skip(start, end, offset)
        elif typ == ObjType.OBJ_OFS_DELTA:
            start, offset_rel = varint(start)
            sha_base = offsstore[offset - offset_rel]
                                        
            if sha_base not in blobstore:
                start, end = skip(start, end, offset)
            else:
                start, end, data = read(start, end)
                data = patch_delta(blobstore[sha_base], data)
                yield handle(typestore[sha_base], data, offset)
        elif typ == ObjType.OBJ_REF_DELTA:
            sha_base = buf[start:start+20].hex()
            start += 20

            if sha_base not in blobstore:
                start, end = skip(start, end, offset)
            else:
                start, end, data = read(start, end)
                data = patch_delta(blobstore[sha_base], data)
                yield handle(typestore[sha_base], data, offset)
        else:
            assert False
        num.left -= 1

        if start > end - MAX_HEADER_SIZE:
            # Make sure that there is always a minimum of MAX_HEADER_SIZE bytes left
            buf[:end-start] = buf[start:end]
            end -= start
            start = 0
            i = f.readinto(buf[end:])
            end += i
            num.rbytes += i

    # Skip the SHA1 checksum
    assert end - start == 20

    print('Commits: %d\nTrees:   %d\nSkipped: %d\nTotal:   %d'
          % (num.commits, num.trees, num.skipped, num.total))
    
def dump(fname, r):
    with open(fname, 'wb') as f:
        while True:
            b = r.read(65536)
            if len(b) == 0: break
            f.write(b)
        f.flush()
        f.close()

def fetch_pack(owner, repo):
    files = get_some_files_hide_errors(owner, repo)[:options.files_max_num]

    h = {'User-Agent': options.user_agent}

    print('Starting pack negotiation... ', end='')
    sys.stdout.flush()
    
    conn = httpc.HTTPSConnection('github.com')
    try:
        conn.request('GET', '/%s/%s.git/info/refs?service=git-upload-pack' % (owner, repo), headers=h)
        it = pkt_line(conn.getresponse().read())
        assert next(it).rstrip(b'\n') == b'# service=git-upload-pack'
        assert next(it) is None

        # Ignore the default ref, will be in the lates ones also
        ref1, cap = next(it).split(b'\0')

        # Don't download all refs, just the first one
        #refs = [i.split(b' ')[0] for i in it if i is not None]
        refs = [ref1.split(b' ')[0]]

        lst = [b'want %s\n' % i for i in refs]
        caps = b'multi_ack_detailed no-done side-band-64k thin-pack ofs-delta agent='
        caps += options.user_agent.encode('ascii')
        lst[0] = lst[0].rstrip(b'\n') + b' ' + caps
        lst.append(None)
        lst += [b'have %s\n' % i.encode('ascii') for i in files]
        lst.append(b'done\n')
        body = mk_pkt_line(lst)

        h1 = {
            'User-Agent': options.user_agent,
            'Accept-Encoding': 'gzip',
            'Content-Type': 'application/x-git-upload-pack-request',
            'Accept': 'application/x-git-upload-pack-result'
        }


        conn.request('POST', '/%s/%s.git/git-upload-pack' % (owner, repo), headers=h1, body=body)
        r = conn.getresponse()
        print('Done.')

        while True:
            num = int(r.read(4), 16)
            assert num != 0
            line = r.read(num - 4).rstrip(b'\n')
            l = line.split(b' ')
            assert len(l) in (1, 2, 3)
            if l[0] == b'NAK': break
            assert l[0] == b'ACK'
            if len(l) == 2: break

        r_stream = Side_band_64k(r)
    except:
        conn.close()
        raise

    #dump('data.cache', r_stream)

    return r_stream

def write_packfile_file(r, fname):
    f = open(fname, 'w+b')

    num = _write_packfile_helper(r, f, -1)
    
    f.seek(8)
    f.write(struct.pack('!I', num))

    h = hashlib.sha1()
    f.seek(0)
    buf = bytearray(4096)
    while True:
        n = f.readinto(buf)
        h.update(buf[:n])
        if n < len(buf): break

    f.write(h.digest())
    f.close()

def write_packfile_stream(r, f):
    _write_packfile_helper(r, f, 0)
    f.write(bytes(21))
    
def _write_packfile_helper(r, f, compression):
    it = objs(r)
    f.write(b'PACK\0\0\0\2\0\0\0\0')
    
    num = 0
    for sha, o in objs(r):
        l = len(o.blob)
        b = (o.typ << 4) | (l & 15)
        l >>= 4
        while l:
            f.write(bytes([b | 128]))
            b = l & 127
            l >>= 7
        f.write(bytes([b]))
        f.write(zlib.compress(o.blob, compression))
        num += 1
    return num

def write_metadata_object(f, owner, repo):
    time_start   = time.clock()

    print('Acquiring %s/%s...' % (owner, repo))
    
    # Write header
    f.write(('REPO %s/%s\0' % (owner, repo)).encode('utf-8'))
    
    r = fetch_pack(owner, repo)
    try:
        write_packfile_stream(r, f)
    finally:
        r.close()

    print('Done. (%.02fs)' % (time.clock() - time_start))

def find_repos_and_offset(f):
    buf = memoryview(global_64k_buffer)
    repos = []
    offset_last = 0

    def at_end(start, end, rbyte, l):
        assert l < len(buf)
        if start + l > end:
            buf[0:end-start] = buf[start:end]
            end -= start
            start = 0
            b = f.readinto(buf[end:])
            rbyte += b
            end += b
            if start + l > end:
                return start, end, rbyte, True
        return start, end, rbyte, False

    start = 0
    rbyte = end = f.readinto(buf)
    
    while True:
        start, end, rbyte, c = at_end(start, end, rbyte, 100)
        if c: break
                
        # Find the next repo
        assert buf[start:start+5] == b'REPO '
        start += 5

        i = buf[start:start+95].tobytes().find(b'\0')
        owner, repo = buf[start:start+i].tobytes().decode('utf-8').split('/')
        start += i + 1
        
        assert buf[start:start+8] == b'PACK\0\0\0\2' 
        start += 12

        flag = True
        while flag:
            start, end, rbyte, c = at_end(start, end, rbyte, 21)
            if c: flag = False; break
                
            # Read the object
            typ, size, start = objhead(buf, start)
            if typ == ObjType.OBJ_NONE: break
            assert typ in (ObjType.OBJ_COMMIT, ObjType.OBJ_TREE)
            
            o = zlib.decompressobj()
            while True:
                start, end, rbyte, c = at_end(start, end, rbyte, 20)
                if c: flag = False; break
                o.decompress(buf[start:end])
                start = end - len(o.unused_data)
                if o.eof: break

        # Skip checksum
        start, end, rbyte, c = at_end(start, end, rbyte, 20)
        if c: flag = False; break
        start += 20

        if not flag: break
                    
        repos.append((owner, repo))
        offset_last = rbyte - (end - start)
        print('Found repository %s/%s' % (owner, repo))
        
    return repos, offset_last

def copy_bytes(fr, to, rbyte):
    buf = memoryview(global_64k_buffer)
    time_last = time.clock()
    i = 0
    while i < rbyte:
        if time.clock() > time_last + 1:
            time_last = time.clock()
            print('Copying... (%2.02f%%)' % (i / rbyte * 100))
        
        num = fr.readinto(buf)
        towrite = min(rbyte - i, len(buf))
        assert towrite <= num
        to.write(buf[:towrite])
        i += towrite
    assert i == rbyte

# Quick hack for repositories that break alarm. Currently only this one.
repos_to_skip = [('Homebrew', 'legacy-homebrew')]

def acquire_metadata(fname, repos_arg, idx):
    dname = os.path.basename(fname)

    repos = []
    for i in repos_arg:
        if i in idx.repos:
            print(f"Skipping repository {'/'.join(i)}, already exists in file {idx.repos[i]}")
            continue
        if i in repos_to_skip:
            print(f"Skipping repository {'/'.join(i)}")
            continue
        repos.append(i)

    if not repos:
        print('No repositories left to acquire.')
        return
    
    f = None
    if os.path.exists(fname):
        print('Found already existing file %s' % fname)
        i = 0
        while True:
            # Note that the directory of fname is preserved
            fname2 = '%s.bak.%d' % (fname, i)
            if not os.path.exists(fname2): break
            i += 1
        os.rename(fname, fname2)
        f2 = gzip.open(fname2, 'rb')

        if dname in idx.files:
            print(f'File {dname} is in the index, skipping right ahead...')
            offset = idx.files[dname][Index.F_OFFSET]
            repos_have = [i for i, v in idx.repos.items() if v == dname]
            
            # Copying the whole file is, quite frankly, ludicrously inefficient. Sadly I do not see
            # an easy way to avoid it.
            f1 = io.BufferedWriter(gzip.open(fname, 'xb', compresslevel=5))
            copy_bytes(f2, f1, offset)
            f2.close()
            
            os.remove(fname2)
            
            f = f1
        elif f2.read(4) != ALARMFILE_MAGIC:
            f2.close()
            print('File is not an alarmfile, has been moved to %s' % fname2)
        else:
            print('Detected alarmfile, trying to resume download...')

            repos_have, offset = find_repos_and_offset(f2)
            f2.close()
            offset += 4 # take care to include the magic

            warnflag = False
            if not repos_have:
                print('Warning: No repositories found.')
                warnflag = True
            else:
                print('Found %d repositories.' % len(repos_have))

                # see above
                f1 = io.BufferedWriter(gzip.open(fname, 'xb', compresslevel=5))
                f2 = gzip.open(fname2, 'rb')            
                copy_bytes(f2, f1, offset)
                f2.close()
                
                idx.setfile(dname, os.path.getsize(fname), offset, repos_have)
                save_index(idx)
                
                for i in repos_have:
                    if i in repos:
                        repos.remove(i)

            if not warnflag:
                os.remove(fname2)
                f = f1

    if f is None:
        f = io.BufferedWriter(gzip.open(fname, 'xb'))
        f.write(ALARMFILE_MAGIC)
        offset = 0
        repos_have = []

    try:
        for owner, repo in repos:
            write_metadata_object(f, owner, repo)
            offset = f.tell()
            repos_have.append((owner, repo))

            if global_stop_flag: break
    finally:
        f.close()
        if offset:
            dname = os.path.basename(fname)
            idx.setfile(dname, os.path.getsize(fname), offset, repos_have)
        save_index(idx)

def fileify(s):
    return ''.join(i for i in s.lower() if i not in ' /\\?*:|"\'<>' and i.isprintable())    

class Index:
    F_SIZE = 0
    F_OFFSET = 1
    
    def __init__(self):
        self.files = {} # maps file -> (size, offset)
        self.repos = {} # maps repo -> file

    def setfile(self, dname, size, offset, repos):
        self.files[dname] = size, offset
        for i in repos:
            if i in self.repos and self.repos[i] != dname:
                print(f'Warning: Repository {i} is contained in both {dname} and {self.repos[i]}')
            else:
                self.repos[i] = dname

def init_index(also_rebuild=False):
    data_dir = options.data
    if not os.path.isdir(data_dir):
        die(f'{data_dir} does not exist or is not a directory')

    idx = Index()
    idx.fname = os.path.join(options.data, options.index)
             
    if os.path.isdir(idx.fname):
        die(f'{idx_fname} is a directory, was supposed to be an indexfile')
        
    if os.path.isfile(idx.fname):
        with open(idx.fname, 'r') as f:
            data = json.loads(f.read())

        idx.files = data['files']
        idx.repos = {tuple(i.split('/')): j for i,j in data['repos'].items()}
        
    files = [i for i in os.listdir(data_dir) if (i.endswith('.alarm.gz')
        and os.path.isfile(os.path.join(data_dir, i)))]
    if also_rebuild:
        print(f'Found {len(files)} files to index')

    up_to_date = set()
    for dname in list(idx.files):
        fname = os.path.join(data_dir, dname)
        good = os.path.exists(fname) and idx.files[dname][Index.F_SIZE] == os.path.getsize(fname)
        if good:
            if also_rebuild:
                print(f'File {dname} is already indexed, no changes detected')
            up_to_date.add(dname)
        else:
            del idx.files[dname]
    idx.repos = {k: v for k, v in idx.repos.items() if v in up_to_date}

    if also_rebuild:
        for dname in files:
            fname = os.path.join(data_dir, dname)
            if dname in up_to_date: continue
            print(f'Currently indexing {fname}...')
            with gzip.open(fname, 'rb') as f:
                assert f.read(4) == ALARMFILE_MAGIC
                repos, offset = find_repos_and_offset(f)

            idx.setfile(dname, os.path.getsize(fname), offset, repos)

        save_index(idx)

    return idx

def save_index(idx):
    with open(idx.fname, 'w') as f:
        repos = {'/'.join(i): j for i,j in idx.repos.items()}
        f.write(json.dumps({'files': idx.files, 'repos': repos}, indent = 4))
            
def cmd_genindex():
    init_index(True)

def cmd_acquire(dname, *repos_str):
    data_dir = options.data

    if not dname.endswith('.alarm.gz'):
        print(f'Warning: {dname} does not end with .alarm.gz, adding it')
        dname += '.alarm.gz'
    fname = os.path.join(data_dir, dname)
        
    repos = []
    for i in repos_str:
        on = tuple(i.split('/'))
        if len(on) != 2:
            die(f'Each repository must be in the form <owner>/<name>, got {i}')
        repos.append(on)

    if not os.path.exists(data_dir):
        print(f'{data_dir} does not exist, will be created')
        os.makedirs(data_dir)
        
    idx = init_index()
    init_github_api()

    acquire_metadata(fname, repos, idx)
        
def cmd_by_language(lang_file):
    data_dir = options.data
    
    if not os.path.isfile(lang_file):
        die(f'{lang_file} does not exist or is not a file')

    if not os.path.exists(data_dir):
        print(f'{data_dir} does not exist, will be created')
        os.makedirs(data_dir)

    idx = init_index()
    init_github_api()
        
    with open(lang_file, 'r') as f:
        langs = [j for j in (i.strip() for i in f.read().splitlines()) if j and j[0] != '#']
        
    for lang in langs:
        repos = get_top100_for_language(lang)
        dname = 'top100_%s.alarm.gz' % fileify(lang)
        acquire_metadata(os.path.join(data_dir, dname), repos, idx)
        
        if global_stop_flag: break

def cmd_small(startpage=1):
    data_dir = options.data

    if not os.path.exists(data_dir):
        print(f'{data_dir} does not exist, will be created')
        os.makedirs(data_dir)

    idx = init_index()
    init_github_api()

    page = int(startpage)
    while True:
        repos = get_small_repos(page)
        dname = f'small_page{page}.alarm.gz'
        acquire_metadata(os.path.join(data_dir, dname), repos, idx)
        
        if global_stop_flag: break
        page += 1

class options:
    AT_LEAST_ONE = object()
    AT_MOST_ONE = object()
    
    _arg_1 = {
        'data':           ('d', str, 'data'),
        'index':          ('i', str, ALARM_INDEX_NAME),
        'token_file':     ('t', str, 'token'),
        'files_max_refs': ('B', int, 1),
        'files_max_num':  ('F', int, 5000),
        'small_min':      ('m', int, 10000),
        'small_max':      ('M', int, 100000),
        'user_agent':     ('u', str, f'alarm/{ALARM_VERSION}'),
    }
    _commands = {
        'acquire': AT_LEAST_ONE,
        'by_language': 1,
        'small': AT_MOST_ONE,
        'genindex': 0,
    }

    @classmethod
    def init(cls):
        cls.argmap = {}
        for name, (short, _, val) in cls._arg_1.items():
            setattr(cls, name, val)
            op_s = '-' + short
            assert op_s not in cls.argmap
            cls.argmap[op_s] = name
            cls.argmap['--' + name.replace('_', '-')] = name

    @classmethod
    def describe(cls, name):
        short, _, val = cls._arg_1[name]
        return f"--{name.replace('_', '-')},-{short} <arg> [default: {val}]"

    @classmethod
    def set(cls, name, val):
        setattr(cls, name, cls._arg_1[name][1](val))
        
    
def print_usage(f = sys.stdout):
    s = f'''\
Usage: {sys.argv[0]} [options...] command [args...]
        
# Commands

  acquire <file> <repo> [<repo> ...]
    Acquire the repositories and write them into <file>. If <file> is an alarmfile, the data will \
be appended. Else, it will be moved away. If an index exist, it will be used to skip already \
downloaded repositories. <file> should be specified relative to the data directory. Each <repo> \
should be of the form <owner>/<name>.

  by_language <lst>
    Acquire the top100 repositories for the languages specified in the file <lst>, in the same way as \
the command acquire.

  small
    Acquire small repositories into the data directory, in the same way as the command acquire.

  genindex
    Generate an index for the files in the data directory. If an index already exists, it is updated.

# Options

  {options.describe('data')}
    Location of the data directory. Most things happen relative to the data directory.

  {options.describe('index')}
    Name of the index file.

  {options.describe('token_file')}
    File to read the GitHub API token from.

  {options.describe('files_max_refs')}
    Maximum number of refs to load when prefetching files.

  {options.describe('files_max_num')}
    Maximum number of prefetched files that will be passed to the server while negotiating packs. \
(Bigger files are passed first.)

  {options.describe('small_min')}
    Minimum size of a repository to be considered small (in KiB).

  {options.describe('small_max')}
    Maximum size of a repository to be considered small (in KiB).

  {options.describe('user_agent')}
    String to send as user-agent in both API and pack-negotiation requests.

  --help,-h
    Print this help and exit.

  --version,-v
    Print the version of alarm and exit. (Currently: {ALARM_VERSION})

'''
    width = shutil.get_terminal_size()[0] - 2
    for l in s.splitlines():
        pre = 0
        while pre < len(l) and l[pre] == ' ': pre += 1
        l = textwrap.fill(l, width=width, subsequent_indent=' '*pre, replace_whitespace=False)
        f.write(l.rstrip() + '\n')

def print_version(f):
    f.write(f'alarm, version {ALARM_VERSION}\nwritten by Philipp Czerner')

class Arg_parse_error(Exception): pass
    
def parse_cmdline(args):
    args = list(args[1:])
    args.reverse()
    
    def pop(name):
        if not args:
            raise Arg_parse_error(f'Unexpected end of arguments, expected {name}')
        return args.pop()

    state = 0
    while True:
        arg = pop('an option or command')

        if arg in ('--help', '-h', '-?', '/?', '/h', '/H'):
            print_usage(sys.stdout)
            sys.exit(2)
            
        if arg in ('--version', '-v'):
            print_version(sys.stdout)
            sys.exit(2)
                        
        if arg.startswith('-'):
            op = arg
            if op not in options.argmap:
                raise Arg_parse_error(f'Unknown option {op}')
            options.set(options.argmap[op], pop('the argument to ' + op))
        else:
            cmd = arg
            if cmd not in options._commands:
                cmds = ', '.join(options._commands)
                raise Arg_parse_error(f'Unknown command {cmd}, must be one of {cmds}')

            cmd_args = []
            num_args = options._commands[cmd]
            
            if num_args is options.AT_LEAST_ONE:
                num_args = max(1, len(args))
            if num_args is options.AT_MOST_ONE:
                num_args = min(1, len(args))
                
            for i in range(num_args):
                cmd_args.append(pop(f'argument {i+1} to command {cmd}'))

            if args:
                err = f'Too many arguments: {cmd} expects {num_args}, got {num_args + len(args)}'
                raise Arg_parse_error(err)
            
            return cmd, cmd_args

def die(msg):
    print('Error:', msg, file=sys.stderr)
    sys.exit(3)
        
def main():
    options.init()
    signal.signal(signal.SIGINT, request_stop_handler)
    try:              
        cmd, cmd_args = parse_cmdline(sys.argv)
        {   'acquire':     cmd_acquire,
            'by_language': cmd_by_language,
            'small':       cmd_small,
            'genindex':    cmd_genindex,
        }[cmd](*cmd_args)
    except Arg_parse_error as e:
        print('Error while parsing arguments:', str(e), file=sys.stderr)
        sys.exit(1)

main()
