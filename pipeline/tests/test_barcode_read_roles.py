import logging

import pytest

from scpre.docker_run import ToolError
from scpre.stages import _assign_roles


def fastq(path, sequence, count=300):
    path.write_text(''.join(f'@r{i}\n{sequence}\n+\n'+ 'F'*len(sequence)+'\n' for i in range(count)))


def test_equal_length_5prime_roles_follow_barcode_not_filename(tmp_path):
    barcode='ACGTACGTACGTACGT'
    wl=tmp_path/'whitelist.txt'
    wl.write_text(barcode+'\n')
    a,b=tmp_path/'SRR1_1.fastq',tmp_path/'SRR1_2.fastq'
    fastq(a,'G'*150)
    fastq(b,barcode+'G'*10+'TTTCTTATATGGG'+'G'*111)
    decision={}
    roles=_assign_roles([a,b],logging.getLogger(),wl,decision)
    assert roles[a]=='R2' and roles[b]=='R1'
    assert decision['evidence'][b.name]['matches']==300
    assert decision['whitelist_sha256']


def test_two_barcode_matching_reads_remain_ambiguous(tmp_path):
    barcode='ACGTACGTACGTACGT'
    wl=tmp_path/'whitelist.txt'
    wl.write_text(barcode+'\n')
    a,b=tmp_path/'SRR1_1.fastq',tmp_path/'SRR1_2.fastq'
    fastq(a,barcode+'G'*134)
    fastq(b,barcode+'G'*134)
    with pytest.raises(ToolError,match='cannot distinguish'):
        _assign_roles([a,b],logging.getLogger(),wl,{})


def test_too_few_barcode_reads_do_not_trigger_selection(tmp_path):
    barcode='ACGTACGTACGTACGT'
    wl=tmp_path/'whitelist.txt'
    wl.write_text(barcode+'\n')
    a,b=tmp_path/'SRR1_1.fastq',tmp_path/'SRR1_2.fastq'
    fastq(a,barcode+'G'*134,100)
    fastq(b,'G'*150,100)
    with pytest.raises(ToolError,match='cannot distinguish'):
        _assign_roles([a,b],logging.getLogger(),wl,{})
