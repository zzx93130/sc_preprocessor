import gzip
import logging

import pytest

from scpre.paired_input import prepare, select_pairs


def fastq(path, ids):
    with gzip.open(path, 'wb') as f:
        for n in ids:
            f.write(f'@SRR9985349.{n} {n} length=4\nACGT\n+\nIIII\n'.encode())


def select(tmp_path, a, b, pairs=2, unpaired=2):
    r1, r2, out = (tmp_path / n for n in ('r1.gz', 'r2.gz', 'out.gz'))
    fastq(r1, a)
    fastq(r2, b)
    result = select_pairs(r1, r2, out, 'SRR9985349', pairs, unpaired)
    return result, out


def test_selects_ids_not_positions_and_keeps_originals(tmp_path):
    result, out = select(tmp_path, [1, 2, 3, 4], [2, 4])
    assert result['retained_pairs'] == 2 and result['unpaired_r1'] == 2
    assert gzip.open(out, 'rb').read() == gzip.open(tmp_path / 'r2.gz', 'rb').read()
    assert gzip.open(tmp_path / 'r1.gz', 'rb').read().count(b'@') == 4


@pytest.mark.parametrize('a,b,match', [
    ([1, 3], [1, 2], 'no matching R1'),
    ([1, 1], [1], 'Duplicate or unsorted'),
    ([2, 1], [1], 'no matching R1'),
    ([1], [1, 2], 'no matching R1'),
    ([1, 2], [1, 1], 'Duplicate or unsorted'),
])
def test_bad_pairings_fail_without_publishing(tmp_path, a, b, match):
    with pytest.raises(ValueError, match=match):
        select(tmp_path, a, b)
    assert not (tmp_path / 'out.gz').exists()


def test_wrong_expected_counts_fail(tmp_path):
    with pytest.raises(ValueError, match='Unexpected counts'):
        select(tmp_path, [1, 2], [1, 2])
    assert not (tmp_path / 'out.gz').exists()


def test_rejects_overwriting_source(tmp_path):
    path = tmp_path / 'r1.gz'
    fastq(path, [1])
    before = path.read_bytes()
    with pytest.raises(ValueError, match='overwrite'):
        select_pairs(path, path, path, 'SRR9985349', 1, 0)
    assert path.read_bytes() == before


def test_malformed_or_corrupt_gzip_fails(tmp_path):
    a, b = tmp_path / 'a.gz', tmp_path / 'b.gz'
    fastq(a, [1, 2])
    fastq(b, [1])
    a.write_bytes(a.read_bytes()[:-5])
    with pytest.raises((EOFError, OSError)):
        select_pairs(a, b, tmp_path / 'out.gz', 'SRR9985349', 1, 1)
    assert not (tmp_path / 'out.gz').exists()
    with gzip.open(a, 'wb') as f:
        f.write(b'@SRR9985349.1\nACGT\n+\nII\n')
    with pytest.raises(ValueError, match='Malformed'):
        select_pairs(a, b, tmp_path / 'out.gz', 'SRR9985349', 1, 0)


def test_prepare_preserves_all_inputs_and_verifies_restart(tmp_path):
    source, dest = tmp_path / 'raw', tmp_path / 'analysis'
    source.mkdir()
    for lane, ids in [('L001_R1', [1, 2]), ('L001_R2', [1, 2]),
                      ('L002_R1', [1, 2, 3]), ('L002_R2', [1, 3])]:
        fastq(source / f'GSM_S1_{lane}_001.fastq.gz', ids)
    originals = {p.name: p.read_bytes() for p in source.iterdir()}
    policy = {'L002': dict(run='SRR9985349', expected_pairs=2, expected_unpaired_r1=1)}
    first = prepare(source, dest, 'GSM', policy, logging.getLogger())
    assert first['original_data_preserved']
    assert originals == {p.name: p.read_bytes() for p in source.iterdir()}
    assert first == prepare(source, dest, 'GSM', policy)
    target = dest / 'GSM_S1_L002_R1_001.fastq.gz'
    data = bytearray(target.read_bytes())
    data[-1] ^= 1
    target.write_bytes(data)
    with pytest.raises(ValueError, match='no longer matches'):
        prepare(source, dest, 'GSM', policy)


def test_pipeline_uses_derivative_but_archives_originals(tmp_path, monkeypatch):
    import json
    import zipfile
    from scpre import repro
    from scpre.dryrun import build_fixture_project
    from scpre.docker_run import MockRunner
    from scpre.state import State
    from scpre.stages import StageContext, stage_cellranger, stage_archiving, stage_verifying

    cfg = build_fixture_project(tmp_path)
    cfg.data.setdefault('metadata', {})['paired_only_runs'] = {
        'SRR9985349': dict(expected_pairs=2, expected_unpaired_r1=1)}
    state = State(cfg.state_db)
    state.upsert_sample('GSM', project='default', mode='sra', srrs=['SRR9985349'], meta={})
    runner = MockRunner(cfg)
    ctx = StageContext(cfg, state, runner, state.get_sample('GSM'), logging.getLogger())
    ctx.fastq_dir.mkdir(parents=True)
    r1 = ctx.fastq_dir / 'GSM_S1_L001_R1_001.fastq.gz'
    r2 = ctx.fastq_dir / 'GSM_S1_L001_R2_001.fastq.gz'
    fastq(r1, [1, 2, 3])
    fastq(r2, [1, 3])
    seen = []
    original_count = runner.cellranger_count

    def count(*args):
        seen.append(args[1])
        return original_count(*args)

    monkeypatch.setattr(runner, 'cellranger_count', count)
    monkeypatch.setattr(repro, '_py_packages', lambda: '')
    stage_cellranger(ctx)
    assert seen == [ctx.work / 'cellranger_fastq']
    assert gzip.open(seen[0] / r1.name, 'rb').read().count(b'@') == 2
    assert gzip.open(r1, 'rb').read().count(b'@') == 3
    stage_archiving(ctx)
    stage_verifying(ctx)
    with zipfile.ZipFile(ctx.archive / 'archives' / 'GSM.fastq.zip') as archive:
        assert gzip.decompress(archive.read(r1.name)).count(b'@') == 3
    audit = json.loads((ctx.archive / 'logs' / 'GSM.json').read_text())
    assert all(c['ok'] for c in audit['checks'])
    assert (ctx.archive / 'repro' / 'paired_input.py').is_file()
    script = (ctx.archive / 'repro' / 'reproduce_GSM.sh').read_text()
    assert 'paired_input.py' in script and '"$ANALYSIS_FASTQ":/fastq:ro' in script
