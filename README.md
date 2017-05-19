# alarm
Application to Lazily Acquire Repository Metadata

## Description

Download and parse metadata (commits and trees) of a git repository via http(s). Various functions to query information about repositories from the GitHub API and download their metadata.

This works by using the git smart http protocol. It queries the API once to gather sha1-hashes for some files, and then informs the server that these files already exist locally (falsely). The hope is to catch some large blobs in there and save on bandwidth.

The server sends us a packfile with the repository data, which we parse to extract the metadata (meaning commits and trees). The metadata is then saved to disk.


## File format

Alarm writes `.alarm.gz` files, which hopefully easy to parse and somewhat efficient. The file is gzipped (as you might have guessed already), with the following structure:

~~~~
- A header appears at the beginning, consisting of:

    4-byte magic number: "0\x9e\xb9\x08"
    
- Some number of metadata-objects follow. They start with a header:

    Header: "REPO " + owner + '/' + repo + '\0'

  where both owner and repo do not contain any of ' ', '/', '\0'. The header is
  followed by a packfile-stream. These are *mostly* compatible to git-packfiles
  (see Documentation/technical/pack-format.txt in the git repository), but have
  three critical differences:
  1. The number of objects in the header (bytes 8-11) is always 0, regardless of
     the actual number of objects in the pack.
  2. After the last object in the pack, there is an additional '\0' byte,
     indicating the end of objects. (Note, that this byte is a valid object
     header in a packfile, with type OBJ_NONE and size 0.)
  3. At the location of the supposed SHA1 hash at the end of the stream, there
     are 20 '\0' bytes instead. (This means that the last 21 bytes are zero.)
  
  These differences enable alarm to write continuously, as soon as data is
  available. Additionally, note that all objects are stored via zlib as per the
  git-packfile specification, but are uncompressed (compression level 0). This
  is more efficient, as the whole file is gzipped anyways.

    Packfile-stream: "PACK\0\0\0\2\0\0\0\0", packfile objects, 21 times '\0'
~~~~