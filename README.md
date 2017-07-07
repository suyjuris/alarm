# alarm
Application to Lazily Acquire Repository Metadata

## Description

Download and parse metadata (commits and trees) of a git repository via http(s). Various functions to query information about repositories from the GitHub API and download their metadata.

This works by using the git smart http protocol. It queries the API once to gather sha1-hashes for some files, and then informs the server that these files already exist locally (falsely). The hope is to catch some large blobs in there and save on bandwidth.

The server sends us a packfile with the repository data, which we parse to extract the metadata (meaning commits and trees). The metadata is then saved to disk.

## Usage information

    Usage: alarm.py [options...] command [args...]
    
    # Commands
    
      acquire <target> [<repo> ...]
        Acquire the repositories and write them into <target>. If <target> is an
        alarmfile, the data will be appended. Else, it will be moved away. If an
        index exist, it will be used to skip already downloaded repositories.
        <target> should be specified relative to the data directory. Each <repo>
        should be of the form <owner>/<name>. You can give no repositories to have
        alarm try and repair the file.
    
      acquire_files <target> <file> [<file> ...]
        Acquire the repositories listed in the files <file>, in the same way as
        the command acquire. These should contain one repository per line, in the
        format <owner>/<name> or https://github.com/<owner>/<name> . Lines
        starting with a # will be ignored.
    
      by_language <lst>
        Acquire the top100 repositories for the languages specified in the file
        <lst>, in the same way as the command acquire.
    
      small
        Acquire small repositories into the data directory, in the same way as the
        command acquire.
    
      genindex
        Generate an index for the files in the data directory. If an index already
        exists, it is updated. This operation should not be necessary in normal
        operation.
    
      list_contents <file> <target> [<target> ...]
        Write a list of all repositories contained in the files <target> into
        <file>, in the format <owner>/<repo>, with one repository per line.
        <target> should be specified relative to the data directory. They are
        interpreted as glob-like pattern.
    
      graph_job <file> <tag> [<tag> ...]
        Similar to write_graphs, but only writes a description of the operations
        to be performed into a file.
    
    # Options
    
      --data,-d <arg> [default: data]
        Location of the data directory. Most things happen relative to the data
        directory.
    
      --classes,-c <arg> [default: classes]
        Location of the classes directory. It may contain files that add tags to
        certain repositories. Each file in that directory should have the same
        format as the files for acquire_files and the name <tag>.lst . Then, <tag>
        will be considered a tag of each listed repository.
    
      --index,-i <arg> [default: alarm.idx]
        Name of the index file.
    
      --token-file,-t <arg> [default: token]
        File to read the GitHub API token from.
    
      --files-max-refs,-B <arg> [default: 1]
        Maximum number of refs to load when prefetching files.
    
      --files-max-num,-F <arg> [default: 5000]
        Maximum number of prefetched files that will be passed to the server while
        negotiating packs. (Bigger files are passed first.)
    
      --small-min,-m <arg> [default: 10000]
        Minimum size of a repository to be considered small (in KiB).
    
      --small-max,-M <arg> [default: 100000]
        Maximum size of a repository to be considered small (in KiB).
    
      --user-agent,-u <arg> [default: alarm/0.1]
        String to send as user-agent in both API and pack-negotiation requests.
    
      --help,-h
        Print this help and exit.
    
      --version,-v
        Print the version of alarm and exit. (Currently: 0.1)


## File format

Alarm writes `.alarm.gz` files, which are hopefully easy to parse and somewhat efficient. The file is gzipped (as you might have guessed already), with the following structure:

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
