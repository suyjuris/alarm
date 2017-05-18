
import json
import hashlib
import http.client as httpc
import sys
import time
import zlib

def get_some_files(owner, repo):
    MAX_BRANCHES = 1
    
    h = {'User-Agent': 'suyjuris', 'Accept': 'application/vnd.github.v3+json'}
    conn = httpc.HTTPSConnection('api.github.com')

    def getapi(loc):
        conn.request('GET', loc, headers=h)
        return json.loads(conn.getresponse().read().decode('utf-8'))
    
    commits = {i['object']['sha'] for i in getapi('/repos/%s/%s/git/refs' % (owner, repo)[:MAX_BRANCHES])}
    trees = {getapi('/repos/%s/%s/git/commits/%s' % (owner, repo, i))['tree']['sha'] for i in commits}

    files = set()
    for t in trees:
        data = getapi('/repos/%s/%s/git/trees/%s?recursive=1' % (owner, repo, t))
        files.update(j['sha'] for j in data['tree'] if j['type'] == 'blob')
        
    conn.close()
    
    return files

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

def pkt_line_stream(f):
    buf = memoryview(global_64k_buffer)

    def rd(b):
        assert f.readinto(b) == len(b)
    
    while True:
        rd(buf[:4])
        num = int(buf[:4].tobytes(), 16)
        if num == 0: break
        rd(buf[4:num])
        yield buf[4], buf[5:]

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
    @classmethod
    def parse(cls, b):
        self = cls()
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
    @classmethod
    def parse(cls, b):
        self = cls()
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

# direct copy of patch-delta.c:patch_delta
def patch_delta(src, delta):
    def varint(buf, start):
        i = x = 0
        while True:
            x |= (buf[start+i] & 0x7f) << 7*i
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

def objs(f):
    buf = memoryview(global_64k_buffer)
    class num: pass

    num.skipped = 0
    num.commits = 0
    num.trees   = 0

    time_last = time.clock()

    blobstore = {}
    typestore = {}

    f.readinto(buf[:8])
    assert buf[:4] == b'\0\0\0\2'
    
    num.total = int.from_bytes(buf[4:8], byteorder='big')
    num.left  = num.total

    def skip(start, end):
        while True:
            o.decompress(buf[start:end])
            if o.eof: break
            assert end == len(buf)
            start = 0
            end = f.readinto(buf)
        start = end - len(o.unused_data)
        num.skipped += 1
        return start, end
    
    def read(start, end):
        data = bytearray()
        while True:
            data += o.decompress(buf[start:end])
            if o.eof: break
            assert end == len(buf)
            start = 0
            end = f.readinto(buf)
        start = end - len(o.unused_data)
        return start, end, data

    def handle(typ, data):
        # see sha1_file.c:write_sha1_file_prepare
        h = hashlib.sha1()
        h.update(b'%s %d\0' % (ObjType.typename(typ), len(data)))
        h.update(data)
        sha = h.digest().hex()
        blobstore[sha] = data
        typestore[sha] = typ
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
    while num.left:
        if time.clock() > time_last + 1:
            time_last = time.clock()
            print('Downloading... (%d/%d)' % (num.total - num.left, num.total))
        
        if start == end: break
        typ, size, off = objhead(buf[start:])
        start += off
        o = zlib.decompressobj()

        if typ in (ObjType.OBJ_COMMIT, ObjType.OBJ_TREE):
            start, end, data = read(start, end)
            yield handle(typ, data)
        elif typ == ObjType.OBJ_BLOB:
            start, end = skip(start, end)
        elif typ == ObjType.OBJ_REF_DELTA:
            sha_base = buf[start:start+20].hex()
            start += 20

            if sha_base not in blobstore:
                start, end = skip(start, end)
            else:
                start, end, data = read(start, end)
                data = patch_delta(blobstore[sha_base], data)
                yield handle(typestore[sha_base], data)
        else:
            assert False
        num.left -= 1

        if start == end:
            start = 0
            end = f.readinto(buf)

    print('Commits: %d\nTrees:   %d\nSkipped: %d\nTotal:   %d'
          % (num.commits, num.trees, num.skipped, num.total))

def dump(fname, r):
    with open(fname, 'wb') as f:
        while True:
            b = r.read(4096)
            if not b: break
            f.write(b)

def pmud(fname):
    with open(fname, 'rb') as f:
        return f.read()


    
        
def main(owner, repo):
    time_start   = time.clock()
    
    files = get_some_files(owner, repo)
    #with open('file.cache', 'r') as f:
    #    files = eval(f.read())
    #files = []

    h = {'User-Agent': 'suyjuris'}
    
    conn = httpc.HTTPSConnection('github.com')
    conn.request('GET', '/%s/%s.git/info/refs?service=git-upload-pack' % (owner, repo), headers=h)
    

    it = pkt_line(conn.getresponse().read())
    assert next(it).rstrip(b'\n') == b'# service=git-upload-pack'
    assert next(it) is None

    # Ignore the default ref, will be in the lates ones also
    cap = next(it).split(b'\0')[1].split(b' ')
    refs = [i.split(b' ')[0] for i in it if i is not None]

    lst = [b'want %s\n' % i for i in refs]
    lst[0] = lst[0].rstrip(b'\n') + b'\0%s' % b'multi_ack_detailed no-done side-band-64k thin-pack ofs-delta agent=git/2.10.1.windows.1'
    lst.append(None)
    lst += [b'have %s\n' % i.encode('ascii') for i in files]
    lst.append(b'done\n')
    lst.append(None)     
    body = mk_pkt_line(lst)

    conn.set_debuglevel(0)
    h1 = {
        'User-Agent': 'suyjuris',
        'Accept-Encoding': 'gzip',
        'Content-Type': 'application/x-git-upload-pack-request',
        'Accept': 'application/x-git-upload-pack-result'
    }
    conn.request('POST', '/%s/%s.git/git-upload-pack' % (owner, repo), headers=h1, body=body)
    r = conn.getresponse()

    while True:
        num = r.read(4)
        if num == b'PACK': break
        r.read(int(num, 16) - 4)

    #dump('data.cache', r)
    
    for sha, o in objs(r): pass

    dur = time.clock() - time_start
    print('Done. (%.02fs)' % dur)

def main2():
    r = open('data.cache', 'rb')
                
    for sha, o in objs(r):
        print(sha[:HASH_DETAIL], o)

    
global_64k_buffer = bytearray(64*1024)

#main('niklasf', 'pyson')
main('git', 'git')
#main2()
