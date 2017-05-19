
import json
import hashlib
import gzip
import http.client as httpc
import struct
import sys
import urllib.parse
import time
import zlib

with open('token', 'r') as f:
    API_TOKEN = f.read().strip()

def get_some_files(owner, repo):
    MAX_BRANCHES = 1

    print('Downloading tree information... ', end='')
    sys.stdout.flush()
    
    h = {'User-Agent': 'suyjuris', 'Accept': 'application/vnd.github.v3+json',
         'Authorization': 'token ' + API_TOKEN }
    conn = httpc.HTTPSConnection('api.github.com')

    def getapi(loc):
        conn.request('GET', loc, headers=h)
        return json.loads(conn.getresponse().read().decode('utf-8'))

    data = getapi('/repos/%s/%s/git/refs' % (owner, repo))[:MAX_BRANCHES]
    try:           
        commits = {i['object']['sha'] for i in data}
        trees = {getapi('/repos/%s/%s/git/commits/%s' % (owner, repo, i))['tree']['sha'] for i in commits}
    except:
        print(data)
        raise

    files = set()
    for t in trees:
        data = getapi('/repos/%s/%s/git/trees/%s?recursive=1' % (owner, repo, t))
        files.update(j['sha'] for j in data['tree'] if j['type'] == 'blob')
        
    conn.close()

    print('Done.')
    print('Found %d files' % len(files))
    
    return files

def get_top100_for_language(lang):
    print('Querying top100 repositories for %s... ' % lang, end='')
    sys.stdout.flush()
    
    h = {'User-Agent': 'suyjuris', 'Accept': 'application/vnd.github.v3+json',
         'Authorization': 'token ' + API_TOKEN }
    conn = httpc.HTTPSConnection('api.github.com')

    def getapi(loc):
        conn.request('GET', loc, headers=h)
        return json.loads(conn.getresponse().read().decode('utf-8'))

    params = urllib.parse.urlencode({'q': 'language:"%s"' % lang, 'sort': 'stars', 'per_page': 100})
    data = getapi('/search/repositories?%s' % params)

    print('Done.')
    
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
    
global_64k_buffer = bytearray(64*1024)

def objs(f):
    buf = memoryview(global_64k_buffer)
    class num: pass
    
    MAX_HEADER_SIZE = 256

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
            return sha, Commit.parse(data)
        elif typ == ObjType.OBJ_TREE:
            num.trees += 1
            return sha, Tree.parse(data)
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
    files = get_some_files(owner, repo)
    #files = []
    #with open('file.cache', 'r') as f:
    #    files = eval(f.read())

    h = {'User-Agent': 'suyjuris'}

    print('Starting pack negotiation... ', end='')
    sys.stdout.flush()
    
    conn = httpc.HTTPSConnection('github.com')
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
    caps = b'multi_ack_detailed no-done side-band-64k thin-pack ofs-delta agent=suyjuris'
    lst[0] = lst[0].rstrip(b'\n') + b' ' + caps
    lst.append(None)
    lst += [b'have %s\n' % i.encode('ascii') for i in files]
    lst.append(b'done\n')
    body = mk_pkt_line(lst)

    conn.set_debuglevel(0)
    h1 = {
        'User-Agent': 'suyjuris',
        'Accept-Encoding': 'gzip',
        'Content-Type': 'application/x-git-upload-pack-request',
        'Accept': 'application/x-git-upload-pack-result'
    }

    if 0:
        f = open('request.out', 'wb')
        f.write(body)
        f.close()
        print(' '.join("-H '%s: %s'" % i for i in h1.items()))
        sys.exit(0)
        
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
    f.write(b'PACK\0\0\0\2\xff\xff\xff\xff')
    
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
    write_packfile_stream(r, f)

    dur = time.clock() - time_start
    print('Done. (%.02fs)' % dur)

ALARMFILE_MAGIC = b'0\x9e\xb9\x08'
    
def aquire_metadata(fname, repos):
    f = gzip.open(fname, 'wb')

    f.write(ALARMFILE_MAGIC)
    
    for owner, repo in repos:
        write_metadata_object(f, owner, repo)

    f.close()

def fileify(s):
    return ''.join(i for i in s.lower() if i not in ' /\\?*:|"\'<>' and i.isprintable())    
    
def main():
    with open('top49_languages.txt', 'r') as f:
        langs = [j for j in (i.strip() for i in f.read().splitlines()) if j and j[0] != '#']
        
    for lang in langs:
        repos = get_top100_for_language(lang)
        aquire_metadata('data/top100_%s.alarm.gz' % fileify(lang), repos)


#aquire_metadata('temp.alarm.gz', [('angular', 'angular.js')])
        
main()

#aquire_metadata('temp.alarm.gz', [('git', 'git')])
