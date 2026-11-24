#!/usr/bin/env python3
"""Report read lengths of SRA split FASTQs and suggest a 10x read_map.

Roles are inferred from read length only, so the suggestion is a starting
point: confirm it against SRA/GEO metadata before a long run.
"""

from __future__ import annotations

import argparse
import gzip
import re
import sys
from collections import Counter
from pathlib import Path

ASSAYS = ("rna", "atac")
SPLIT_RE = re.compile(r"^(?P<accession>.+)_(?P<read>\d+)\.fastq(\.gz)?$")


def modal_read_length(path: Path, max_reads: int = 2000) -> int:
    """Return the most common sequence length among the first reads."""
    opener = gzip.open if path.name.endswith(".gz") else open
    lengths: Counter[int] = Counter()
    with opener(path, "rt", encoding="ascii", errors="replace") as handle:
        for index, line in enumerate(handle):
            if index % 4 == 1:
                lengths[len(line.rstrip("\r\n"))] += 1
                if sum(lengths.values()) >= max_reads:
                    break
    if not lengths:
        raise ValueError(f"no reads found in {path}")
    return lengths.most_common(1)[0][0]


def infer_read_map(lengths: dict[str, int], assay: str) -> dict[str, str]:
    """Map SRA read numbers to 10x roles from modal read lengths.

    lengths: {"1": 50, "2": 16, "3": 50}. Raises ValueError when the layout
    does not match the assay unambiguously.
    """
    if assay not in ASSAYS:
        raise ValueError(f"unsupported assay for read-map inference: {assay}")
    ordered = sorted(lengths, key=int)
    index_reads = [n for n in ordered if lengths[n] <= 10]
    rest = [n for n in ordered if n not in index_reads]
    mapping: dict[str, str] = {}

    if assay == "atac":
        # 10x ATAC: R1/R3 are genomic (about 50 bp), R2 is the i5 barcode read
        # (16 bp; 24 bp for Multiome ATAC), I1 is the optional 8 bp i7 index.
        barcode = [n for n in rest if 16 <= lengths[n] <= 24]
        genomic = [n for n in rest if lengths[n] >= 30]
        if len(barcode) != 1 or len(genomic) != 2 or len(index_reads) > 1 or len(barcode) + len(genomic) != len(rest):
            raise ValueError(
                "cannot infer ATAC roles from lengths "
                f"{lengths}: need one 16-24 bp barcode read and two genomic reads. "
                "If only two reads exist, the barcode read was not deposited as FASTQ; "
                "look for the submitter's original BAM/FASTQ files instead."
            )
        mapping[genomic[0]] = "R1"
        mapping[barcode[0]] = "R2"
        mapping[genomic[1]] = "R3"
        if index_reads:
            mapping[index_reads[0]] = "I1"
    else:
        # 10x 3' GEX: R1 is the 26-30 bp barcode+UMI read, R2 the cDNA read.
        barcode = [n for n in rest if 26 <= lengths[n] <= 30]
        cdna = [n for n in rest if lengths[n] > 30]
        if len(barcode) != 1 or len(cdna) != 1 or len(index_reads) > 2 or len(rest) != 2:
            raise ValueError(
                f"cannot infer GEX roles from lengths {lengths}: need one 26-30 bp "
                "barcode/UMI read and one longer cDNA read. Equal-length layouts "
                "(for example 5' PE150) need an explicit read_map."
            )
        mapping[barcode[0]] = "R1"
        mapping[cdna[0]] = "R2"
        for role, number in zip(("I1", "I2"), index_reads):
            mapping[number] = role
    return {number: mapping[number] for number in ordered}


def format_read_map(mapping: dict[str, str]) -> str:
    return ",".join(f"{number}={role}" for number, role in mapping.items())


def accession_lengths(fastq_dir: Path, accession: str) -> dict[str, int]:
    lengths: dict[str, int] = {}
    for path in sorted(fastq_dir.glob(f"{accession}_*.fastq*")):
        match = SPLIT_RE.match(path.name)
        if match and match.group("accession") == accession:
            lengths[match.group("read")] = modal_read_length(path)
    return lengths


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fastq-dir", type=Path, required=True, help="Directory with {SRR}_{N}.fastq[.gz] files")
    parser.add_argument("--accessions", nargs="*", help="Accessions to inspect; default: all found")
    parser.add_argument("--assay", choices=ASSAYS, help="Also print a suggested read_map for this library type")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    accessions = args.accessions or sorted(
        {m.group("accession") for p in args.fastq_dir.iterdir() if (m := SPLIT_RE.match(p.name))}
    )
    if not accessions:
        print(f"no {{SRR}}_{{N}}.fastq[.gz] files in {args.fastq_dir}", file=sys.stderr)
        return 1
    status = 0
    for accession in accessions:
        lengths = accession_lengths(args.fastq_dir, accession)
        if not lengths:
            print(f"{accession}\tno split FASTQs found")
            status = 1
            continue
        shown = " ".join(f"_{number}={length}bp" for number, length in sorted(lengths.items(), key=lambda kv: int(kv[0])))
        line = f"{accession}\t{shown}"
        if args.assay:
            try:
                line += f"\tread_map={format_read_map(infer_read_map(lengths, args.assay))}"
            except ValueError as exc:
                line += f"\tread_map=UNRESOLVED ({exc})"
                status = 1
        print(line)
    return status


if __name__ == "__main__":
    raise SystemExit(main())
