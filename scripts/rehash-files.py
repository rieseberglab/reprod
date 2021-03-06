#!/usr/bin/env python3
"""
   Verifies the digests of files stored on S3 with the data-rehasher lambda.

   if a digest is provided for a given object, the the remote file content is expected
   to match it (and it is considered an error otherwise). if --delete-mismatch is given
   on the CLI, then the object then remote files with digest mismatches are deleted.

   On success, the remote file's metadata will be updated with the verified
   digests. Pass --extra-digest ALGO to request an additional digest to be computed
   on the remote.

   Input syntax (line-oriented):

     s3://path/to/object [md5:MD5URL]

"""
import os
import os.path
import sys
import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
import boto3
import botocore
from urllib.parse import urlparse

import bunnies.lambdas as lambdas
import bunnies

def setup_logging(loglevel=logging.INFO):
    """configure custom logging for the platform"""

    root = logging.getLogger(__name__)
    root.setLevel(loglevel)
    ch = logging.StreamHandler(sys.stderr)
    ch.setLevel(loglevel)
    formatter = logging.Formatter('[%(asctime)s] %(levelname)s - %(message)s')
    ch.setFormatter(formatter)
    root.addHandler(ch)
    #root.propagate = False

log = logging.getLogger(__name__)

def _s3_split_url(objecturl):
    """splits an s3://foo/bar/baz url into bucketname, keyname: ("foo", "bar/baz")

    the keyname may be empty
    """
    return bunnies.utils.s3_split_url(objecturl)

def do_request(batch_no, req):
    """execute one request. tail the logs. wait for completion"""

    tmp_src = _s3_split_url(req['input'])
    cpy_dst = _s3_split_url(req['output'])

    new_req = {
        "src_bucket": tmp_src[0],
        "src_key": tmp_src[1],
        "dst_bucket": cpy_dst[0],
        "dst_key": cpy_dst[1],
        "digests": req["digests"]
    }

    delete_mismatch = req.get('delete_mismatch', False)

    log.info("REQ%s data-rehash request: %s", batch_no, json.dumps(new_req, sort_keys=True, indent=4, separators=(",", ": ")))
    code, response = lambdas.invoke_sync(lambdas.DATA_REHASH, Payload=new_req)
    data = response['Payload'].read().decode("ascii")
    if code != 0:
        raise Exception("data-rehash failed to complete: %s" % (data,))
    data_obj = json.loads(data)
    if data_obj.get('error', None):
        if "mismatch" in data_obj['error']:
            session = boto3.session.Session()
            s3 = session.client('s3', config=botocore.config.Config(read_timeout=300, retries={'max_attempts': 0}))
            log.info("REQ%s deleting mismatchfile: Bucket=%s Key=%s", batch_no, tmp_src[0], tmp_src[1])
            try:
                s3.delete_object(Bucket=tmp_src[0], Key=tmp_src[1])
            except Exception as delete_exc:
                log.error("REQ%s delete failed", exc_info=delete_exc)
        raise Exception("data-rehash returned an error: %s" % (data_obj,))
    return data_obj

def main_handler():
    """do the work. CLI"""

    import argparse

    parser = argparse.ArgumentParser(description=__doc__)

    parser.add_argument("--threads", metavar="THREADS", type=int, default=1)
    parser.add_argument("--delete-mismatch", dest="delete_mismatch", action="store_true", default=False)
    parser.add_argument("--extra-digest", metavar="ALGO", dest="digests", action="append", default=[])

    args = parser.parse_args()

    args.digests = [ dig.lower() for dig in args.digests ]
        
    creds = {}
    creds['username'] = os.environ.get('USERNAME', '')
    creds['password'] = os.environ.get('PASSWORD', '')

    def _parse_line_request(line):
        toks = line.split()
        if len(toks) < 2:
            raise ValueError("not enough parameters")

        url = toks[0]
        outputurl = toks[0] # we update the file in-place

        digests = {}
        for digestarg in toks[1:]:
            dtype, dvalue = digestarg.split(':', maxsplit=1)
            if dtype == "output":
                outputurl = dvalue

            digests[dtype.lower()] = dvalue.lower()

        # specify that extra digests get computed, if needed
        if len(digests) == 0:
            digests['md5'] = None
        for extra_digest in args.digests:
            if extra_digest not in digests:
                digests[extra_digest] = None

        req_obj = {
            'input': url,
            'output': outputurl,
            'digests': digests,
            'delete_mismatch': args.delete_mismatch
        }
        return req_obj

    requests = []
    for lineno, line in enumerate(sys.stdin):
        line = line.strip()
        if line.startswith("#") or not line: continue
        try:
            req = _parse_line_request(line)
            requests.append((lineno+1, req))
        except ValueError as verr:
            log.error("Problem on line %d: %s", lineno+1, str(verr))
            return 1

    futures = []
    log.info("Submitting %d requests with %d threads...", len(requests), args.threads)

    total_count = len(requests)
    request_done_count = 0
    request_error_count = 0
    results = {}
    with ThreadPoolExecutor(max_workers=args.threads) as executor:
        futures = { executor.submit(do_request, lineno, req): (lineno, req) for (lineno, req) in requests }
        for future in as_completed(futures):
            lineno, req = futures[future]
            request_done_count += 1
            try:
                log.info("request on line %d completed.", lineno)
                data = future.result()
                results[lineno] = data
                log.info("line %d request result: %s", lineno, json.dumps(data, sort_keys=True, indent=4, separators=(",", ": ")))
            except Exception as exc:
                log.error("request on line %d generated an exception: %s", lineno, exc, exc_info=exc)
                error_result = dict(req)
                error_result["error"] = str(exc)
                results[lineno] = error_result
                request_error_count += 1

            log.info("progress %4d/%4d done (%8.3f%%). %d error(s) encountered.",
                     request_done_count, total_count, request_done_count * 100.0 / total_count, request_error_count)

    req_results = [results[lineno] for lineno in sorted(results.keys())]

    json.dump(req_results, sys.stdout, sort_keys=True, indent=4, separators=(",", ": "))
    if request_error_count > 0:
        log.error("exiting. encountered %d error(s) out of %d requests.", request_error_count, total_count)
        return 1
    log.info("completed %d requests.", total_count)
    return 0

if __name__ == "__main__":
    setup_logging(logging.DEBUG)
    log.info("Running boto3:%s botocore:%s", boto3.__version__, botocore.__version__)
    bunnies.setup_logging(logging.DEBUG)
    ret = main_handler()
    sys.exit(0 if ret is None else ret)


