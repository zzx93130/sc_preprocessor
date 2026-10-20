from pathlib import Path

from scpre.config import Config
from scpre.space import estimate
from scpre.sra_table import parse


def test_missing_runs_are_included_in_space_estimate(tmp_path):
    table = tmp_path / 'SraRunTable.csv'
    table.write_text('Run,Sample Name,Bytes\nSRR1,GSM1,100\nSRR2,GSM1,200\n')
    meta = parse(table)['GSM1']['meta']
    present = tmp_path / 'SRR1'
    present.write_bytes(b'x' * 100)
    meta['input_paths'] = [str(present)]
    sample = {'id':'GSM1','mode':'sra','meta':meta}
    result = estimate(sample, Config({}, Path(tmp_path)))
    assert result['input'] == 300
    assert result['conversion_peak_est'] == 3000
