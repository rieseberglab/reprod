#!/usr/bin/env python3
# -*- charset: utf-8; -*-

"""
An example reprod pipeline which aligns and merges samples
"""

# framework
import bunnies
import bunnies.runtime
import os
import logging
import argparse

# experiment specific
from snpcalling import InputFile, Align, Merge


def main():

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--debug", action="store_true", default=False, help="turn on debug logging")
    parser.add_argument("--maxattempts", metavar="N", dest="max_attempts", default=2, type=int,
                        help="try to run a job this many times before cancelling it (min 1)")

    args = parser.parse_args()

    if args.debug:
        bunnies.setup_logging(logging.DEBUG)
    else:
        bunnies.setup_logging(logging.INFO)

    bunnies.runtime.add_user_deps(".", "snpcalling", excludes=("__pycache__"))
    bunnies.runtime.add_user_deps(".", "scripts")
    bunnies.runtime.add_user_hook("import snpcalling")
    bunnies.runtime.add_user_hook("snpcalling.setup_logging()")

    # Reference genome
    ha412     = InputFile("s3://rieseberg-references/HA412/genome/Ha412HOv2.0-20181130.fasta")
    ha412_idx = InputFile("s3://rieseberg-references/HA412/genome/Ha412HOv2.0-20181130.fasta.fai")

    # Align files
    a1 = Align(
        sample_name="ANN0830",
        r1=InputFile("https://github.com/rieseberglab/fastq-examples/raw/master/data/HI.4038.002.index_10.ANN0830_R1.fastq.gz",
                     digests=("cfdbedf549fd23685321d7b27fccfb10",)),
        r2=InputFile("https://github.com/rieseberglab/fastq-examples/raw/master/data/HI.4038.002.index_10.ANN0830_R2.fastq.gz",
                     digests=("397c364cbad6cb16377f5572b89ec5c5",)),
        ref=ha412,
        ref_idx=ha412_idx)

    a2 = Align(
        sample_name="ANN0830",
        r1=InputFile("https://github.com/rieseberglab/fastq-examples/raw/master/data/HI.4549.004.index_10.ANN0830_R1.fastq.gz",
                     digests=("f646412d9568e0c7f1822b951ccc2e6e",)),
        r2=InputFile("https://github.com/rieseberglab/fastq-examples/raw/master/data/HI.4549.004.index_10.ANN0830_R2.fastq.gz",
                     digests=("73ea5780ff055c35d1ac939e73c47580",)),
        ref=ha412,
        ref_idx=ha412_idx)

    a3 = Align(
        sample_name="ANN0832",
        r1=InputFile("https://github.com/rieseberglab/fastq-examples/raw/master/data/HI.4019.002.index_8.ANN0832_R1.fastq.gz",
                     digests=("d841ccf568e94aec99418f232db4535a",)),
        r2=InputFile("https://github.com/rieseberglab/fastq-examples/raw/master/data/HI.4019.002.index_8.ANN0832_R2.fastq.gz",
                     digests=("41720b0a79e20dd81c8865d9404cd550",)),
        ref=ha412,
        ref_idx=ha412_idx)

    # Transform objects form a graph
    all_bams = [a1, a2, a3]

    # merge them by key
    merged_bam1 = Merge("ANN0830", [bam for bam in all_bams if bam.sample_name == "ANN0830"])
    merged_bam2 = Merge("ANN0832", [bam for bam in all_bams if bam.sample_name == "ANN0832"])

    all_merged = [merged_bam1, merged_bam2]

    # - fixates software versions and parameters
    # - creates graph of dependencies
    pipeline = bunnies.build_pipeline(all_merged)

    # TODO - a URL where we can see details and progress in the browser (maybe via lambda+apigateway)
    # print(pipeline.dashboard_url())

    #
    # Tag all entities with the name of the program
    #
    pipeline.build(os.path.basename(__file__), local_scratch_gb=100, max_attempts=args.max_attempts)

    for target in pipeline.targets:
        print(target.data.exists())


if __name__ == "__main__":
    import sys
    sys.exit(main() or 0)
