"""Audited, opt-in SRA paired-only derivatives; never modify raw inputs."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import logging
import re
import shutil
from pathlib import Path


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda: f.read(8 * 1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def _records(path, run):
    previous = 0
    with gzip.open(path, 'rb') as f:
        while True:
            header = f.readline()
            if not header:
                return
            seq, plus, qual = f.readline(), f.readline(), f.readline()
            if (not qual.endswith(b'\n') or not plus.startswith(b'+')
                    or not seq.rstrip(b'\r\n')
                    or len(seq.rstrip(b'\r\n')) != len(qual.rstrip(b'\r\n'))):
                raise ValueError(f'Malformed FASTQ in {path}: {header[:100]!r}')
            match = re.match(rb'^@([SED]RR\d+)\.(\d+)(?:\s|/|$)', header)
            if not match or match[1].decode() != run:
                raise ValueError(f'Unexpected SRA identifier in {path}: {header[:100]!r}')
            spot = int(match[2])
            if spot <= previous:
                raise ValueError(f'Duplicate or unsorted spot in {path}: {spot}')
            previous = spot
            yield spot, (header, seq, plus, qual)


def select_pairs(r1, r2, output, run, expected_pairs, expected_unpaired_r1,
                 log=None):
    """Match every spot, validate both gzip streams, retain R1 matches only.

    R2 is copied byte-for-byte by prepare(); unmatched R2 is an error, not a
    license to silently filter both ends. All source files remain untouched.
    """
    log = log or logging.getLogger(__name__)
    output = Path(output)
    if output.resolve() in (Path(r1).resolve(), Path(r2).resolve()):
        raise ValueError('Derivative must not overwrite an original FASTQ')
    output.parent.mkdir(parents=True, exist_ok=True)
    part = output.with_name(output.name + '.part')
    pairs = unpaired = total = 0
    mates = _records(r2, run)
    try:
        mate = next(mates, None)
        with part.open('wb') as raw:
            with gzip.GzipFile(filename='', mode='wb', fileobj=raw,
                               compresslevel=1, mtime=0) as out:
                for spot, record in _records(r1, run):
                    total += 1
                    if mate is not None and mate[0] < spot:
                        raise ValueError(f'R2 spot {mate[0]} has no matching R1')
                    if mate is not None and spot == mate[0]:
                        out.writelines(record)
                        pairs += 1
                        mate = next(mates, None)
                    else:
                        unpaired += 1
                    if total % 1000000 == 0:
                        log.info('paired-only %s: checked %s R1, kept %s pairs, '
                                 'unpaired %s', run, total, pairs, unpaired)
        if mate is not None:
            raise ValueError(f'R2 spot {mate[0]} has no matching R1')
        if pairs != expected_pairs or unpaired != expected_unpaired_r1 or not pairs:
            raise ValueError(f'Unexpected counts for {run}: pairs={pairs}, '
                             f'unpaired_R1={unpaired}; expected '
                             f'{expected_pairs}/{expected_unpaired_r1}')
        part.replace(output)
    finally:
        mates.close()
    return dict(run=run, r1_total=total, r2_total=pairs,
                retained_pairs=pairs, unpaired_r1=unpaired,
                policy='retain originals; select matching SRA spot IDs for analysis')


def prepare(source, destination, sample, lanes, log=None):
    source, destination = Path(source), Path(destination)
    if source.resolve() == destination.resolve():
        raise ValueError('Analysis and original directories must differ')
    log = log or logging.getLogger(__name__)
    originals = sorted(source.glob('*.fastq.gz'))
    if not originals:
        raise ValueError('No original FASTQs')
    destination.mkdir(parents=True, exist_ok=True)
    manifest = destination / 'paired-input-decision.json'
    # Content hashes, not just sentinel sizes, protect restart reuse.
    source_info = {}
    for p in originals:
        log.info('Fingerprinting preserved original %s (%s bytes)', p.name, p.stat().st_size)
        source_info[p.name] = dict(size=p.stat().st_size, sha256=digest(p))
    if manifest.is_file():
        old = json.loads(manifest.read_text())
        if (old.get('sources') == source_info and old.get('lanes') == lanes
                and old.get('sample') == sample
                and {p.name for p in destination.glob('*.fastq.gz')} == set(source_info)
                and set(old.get('outputs', {})) == set(source_info)
                and all((destination / n).is_file()
                        and (destination / n).stat().st_size == info['size']
                        and digest(destination / n) == info['sha256']
                        for n, info in old['outputs'].items())):
            log.info('Verified paired-only derivative reused: %s', destination)
            return old
        raise ValueError('Existing paired-only manifest no longer matches; inspect before replacing')
    decisions = {}
    for lane, policy in lanes.items():
        r1 = source / f'{sample}_S1_{lane}_R1_001.fastq.gz'
        r2 = source / f'{sample}_S1_{lane}_R2_001.fastq.gz'
        decisions[lane] = select_pairs(
            r1, r2, destination / r1.name, policy['run'],
            policy['expected_pairs'], policy['expected_unpaired_r1'], log)
    selected = {f'{sample}_S1_{lane}_R1_001.fastq.gz' for lane in lanes}
    for original in originals:
        if original.name not in selected:
            target = destination / original.name
            part = target.with_name(target.name + '.part')
            log.info('Copying unchanged analysis FASTQ %s', original.name)
            shutil.copyfile(original, part)
            if digest(part) != source_info[original.name]['sha256']:
                raise ValueError(f'Analysis copy checksum mismatch: {original}')
            part.replace(target)
    outputs = {p.name: dict(size=(destination / p.name).stat().st_size,
                           sha256=digest(destination / p.name)) for p in originals}
    if {p.name for p in destination.glob('*.fastq.gz')} != set(source_info):
        raise ValueError('Unexpected extra analysis FASTQ; inspect before using directory')
    # Detect source mutations during preparation; never publish an audit for
    # a mixed snapshot. No original source is deleted, renamed, or rewritten.
    for p in originals:
        if p.stat().st_size != source_info[p.name]['size'] or digest(p) != source_info[p.name]['sha256']:
            raise ValueError(f'Original changed during preparation: {p}')
    record = dict(sample=sample, lanes=lanes, decisions=decisions,
                  sources=source_info, outputs=outputs, original_data_preserved=True)
    temp = manifest.with_suffix('.json.part')
    temp.write_text(json.dumps(record, indent=2))
    temp.replace(manifest)
    return record


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True)
    parser.add_argument('--destination', required=True)
    parser.add_argument('--sample', required=True)
    parser.add_argument('--lanes-json', required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
    prepare(args.source, args.destination, args.sample,
            json.loads(args.lanes_json))
