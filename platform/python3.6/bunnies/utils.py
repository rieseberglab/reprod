"""
misc utilities
"""

import os
import os.path
import errno
from urllib.parse import urlparse
import hashlib
import json
import logging
import boto3
import base64
import glob
import fnmatch
import subprocess

from botocore.exceptions import ClientError
from .exc import NoSuchFile

logger = logging.getLogger(__package__)


def data_files(globname):
    """retrieves filenames under the data directory, matching the given file glob pattern (relative to the data dir)"""
    here = os.path.dirname(__file__)
    data_dir = os.path.join(here, "data")
    matches = [permfile for permfile in glob.glob(os.path.join(data_dir, globname))]
    return matches

def find_config_file(startdir, filname):
    """recurse in folder and parents to find filname and open it
       returns (fd, path)
    """
    parent = os.path.dirname(startdir)
    try:
        attempt = os.path.join(startdir, filname)
        return open(attempt, "r"), attempt
    except IOError as ioe:
        if ioe.errno != errno.ENOENT:
            raise
    # reached /
    if parent == startdir:
        return None, None

    return find_config_file(parent, filname)


def user_region():
    """get the effective profile's region"""
    # follow up: https://stackoverflow.com/questions/56502789/obtaining-the-boto3-profile-settings-from-python/56502829#56502829
    session = boto3.session.Session()
    return session.region_name


def s3_split_url(objecturl):
    """splits an s3://foo/bar/baz url into bucketname, keyname: ("foo", "bar/baz")

    the keyname may be empty
    """
    o = urlparse(objecturl)
    if o.scheme != "s3":
        raise ValueError("not an s3 url")

    keyname = o.path
    bucketname = o.netloc

    # all URL paths start with /. strip it.
    if keyname.startswith("/"):
        keyname = keyname[1:]

    return bucketname, keyname


def get_blob_meta(objecturl, logprefix=""):
    """fetches metadata about the given object. if the object doesn't exist. raise NoSuchFile"""
    bucketname, keyname = s3_split_url(objecturl)
    logprefix = logprefix + " " if logprefix else logprefix
    logger.debug("%sfetching meta for URL: %s", logprefix, objecturl)
    s3 = boto3.client('s3')
    try:
        head_res = s3.head_object(Bucket=bucketname, Key=keyname)
    except ClientError as clierr:
        if clierr.response['Error']['Code'] == '404':
            raise NoSuchFile(objecturl)
        logger.error("%scould not fetch URL (%s): %s", logprefix, repr(clierr.response['Error']['Code']), objecturl,
                     exc_info=clierr)
        raise
    return head_res


class StreamingBodyCxt(object):
    __slots__ = ("res", "body")

    def __init__(self, res):
        self.res = res
        self.body = res['Body']

    def __enter__(self):
        return self.body, self.res

    def __exit_(self, typ, value, traceback):
        self.body.close()


def get_blob_ctx(objecturl, logprefix=""):
    """returns (body, info) for a given blob url.
       It takes care of closing the connection automatically.

    >>> with get_blob_ctx("s3://foo/bar") as (body, info):
    ...    data = body.read()

    """
    bucketname, keyname = s3_split_url(objecturl)
    logprefix = logprefix + " " if logprefix else logprefix
    logger.info("%sfetching URL: %s", logprefix, objecturl)
    s3 = boto3.client('s3')
    try:
        res = s3.get_object(Bucket=bucketname, Key=keyname)
    except ClientError as clierr:
        if clierr.response['Error']['Code'] == '404':
            raise NoSuchFile(objecturl)
        logger.error("%scould not fetch URL (%s): %s", logprefix, repr(clierr.response['Error']['Code']), objecturl,
            exc_info=clierr)
        raise
    return StreamingBodyCtx(res)


def canonical_hash(canon_obj, algo='sha1'):
    """hash a canonical dictionary representation into a hexdigest.

    contained objects must be JSONSerializable, and strings must be unicode, otherwise a TypeError is raised.
    """
    serialized = json.dumps(canon_obj, sort_keys=True, separators=(",",  ":")).encode('utf-8')
    digest_obj = getattr(hashlib, algo)()
    digest_obj.update(serialized)
    return "%s_%s" % (algo, digest_obj.hexdigest())

def load_json(obj):
    if isinstance(obj, str):
        return json.loads(obj)
    elif isinstance(obj, bytes):
        return json.loads(obj.decode('utf-8'))
    elif hasattr(obj, "read"):
        return json.load(data)
    else:
        raise TypeError("cannot load json from this object")

def parse_digests(digests):
    """
    digests is either a single string digest,
    a list of string digests, or a dictionary of algo:hexdigest keypairs.

    string digest forms supported:

      1) "d41d8cd98f00b204e9800998ecf8427e"
      2) "md5:d41d8cd98f00b204e9800998ecf8427e"
      3) "md5_d41d8cd98f00b204e9800998ecf8427e"
      4) "hash://md5/d41d8cd98f00b204e9800998ecf8427e"
      algorithmic prefixes are ignored. algorithm
      deduced from hexdigest length

    returns a dictionary {'algo': 'hexdigest'}
    """
    def _atom(orig):
        s = orig
        if s.startswith("hash://"):
            s = os.path.split(s[len("hash://"):])[1]
        if ':' in s:
            # e.g. "md5:asdaddas"
            s = s.split(':')[-1]
        if '_' in s:
            # e.g. "sha1_asdsads"
            s = s.split('_')[-1]
        s = s.lower()
        res = {32: ('md5', s),
               40: ('sha1', s),
               64: ('sha256', s),
               128: ('sha512', s)}.get(len(s), None)
        if not res:
            raise ValueError("invalid digest string: %s" % (orig,))
        return res

    if isinstance(digests, (dict,)):
        return dict([_atom(v) for v in digests.values()])
    if not isinstance(digests, (tuple, list)):
        digests = (digests,)
    return dict([_atom(digest) for digest in digests])


def hex2b64(hexstr):
    if len(hexstr) % 2 != 0:
        raise ValueError("Invalid hexstring")
    hexbits = bytes([(int(hexstr[i], 16) << 4) + int(hexstr[i+1], 16) for i in range(0, len(hexstr), 2)])
    return base64.b64encode(hexbits).decode('ascii')


def walk_tree(rootdir, excludes=(), exclude_patterns=()):
    """
    yield files under rootdir, recursively, including empty folders, but
    excluding special files in excludes, or those matching globs in exclude_patterns
    """
    def _is_excluded(comp):
        return (comp in excludes) or any([fnmatch.fnmatchcase(comp, patt) for patt in exclude_patterns])

    def _get_entry(dirname, basename):
        if _is_excluded(basename):
            return

        return os.path.join(dirname, basename)

    for parent, dirs, files in os.walk(rootdir):
        if any([_is_excluded(comp) for comp in os.path.split(parent)]):
            continue

        for basename in files:
            entry = _get_entry(parent, basename)
            if entry:
                yield entry
        for basename in dirs:
            entry = _get_entry(parent, basename)
            if entry:
                yield entry


def run_cmd(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, log_on_err=True,
            show_out=False, cwd=None, log_cmd=True, **kwargs):
    """run a command -- wrapper around subprocess """

    if cwd and cwd != ".":
        if log_cmd:
            logger.debug("+CMD (cwd %s) %s", cwd, " ".join([repr(x) for x in args]))
    else:
        if log_cmd:
            logger.debug("+CMD %s", " ".join([repr(x) for x in args]))

    proc = subprocess.run(args, stdout=stdout, stderr=stderr, check=False, cwd=cwd, **kwargs)
    if proc.returncode != 0 and log_on_err:
        logger.error("command returned code %d", proc.returncode)
        if stdout == subprocess.PIPE:
            for line in proc.stdout.decode('utf-8').splitlines():
                logger.error("out: %s", line)
        if stderr == subprocess.PIPE:
            for line in proc.stderr.decode('utf-8').splitlines():
                logger.error("err: %s", line)
    if proc.returncode == 0 and show_out and stdout == subprocess.PIPE:
        for line in proc.stdout.decode('utf-8').splitlines():
            logger.error("out: %s", line)

    if check:
        proc.check_returncode()
    return proc
