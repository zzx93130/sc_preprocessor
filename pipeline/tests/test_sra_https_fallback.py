import logging
import subprocess
from unittest.mock import Mock

import pytest

from scpre.config import Config
from scpre.docker_run import DockerRunner, ToolError


def make_runner(tmp_path, monkeypatch):
    runner = DockerRunner(Config({}, tmp_path))
    monkeypatch.setattr('scpre.docker_run.shutil.which', lambda name: 'curl.exe')
    monkeypatch.setattr('scpre.docker_run.subprocess.run', lambda *a, **kw:
                        subprocess.CompletedProcess([], 0, 'Content-Length: 12\r\n', ''))
    return runner


def test_verified_partial_is_validated_before_publish(tmp_path, monkeypatch):
    runner = make_runner(tmp_path, monkeypatch)
    part = tmp_path / 'SRR1.sra.part'
    part.write_bytes(b'NCBI.sra1234')
    def validate(*args):
        assert part.exists()
        assert not (tmp_path / 'SRR1.sra').exists()
        assert args[3] == ['vdb-validate', '/in/SRR1.sra.part']
    runner._run = Mock(side_effect=validate)
    result = runner._prefetch_public_s3('SRR1', tmp_path, 'GSM1', logging.getLogger())
    assert result.read_bytes() == b'NCBI.sra1234'
    assert not part.exists()


def test_invalid_payload_is_not_published(tmp_path, monkeypatch):
    runner = make_runner(tmp_path, monkeypatch)
    (tmp_path / 'SRR1.sra.part').write_bytes(b'<html>123456')
    runner._run = Mock()
    with pytest.raises(ToolError, match='invalid archive'):
        runner._prefetch_public_s3('SRR1', tmp_path, 'GSM1', logging.getLogger())
    runner._run.assert_not_called()
    assert not (tmp_path / 'SRR1.sra').exists()


def test_failed_validation_preserves_partial_only(tmp_path, monkeypatch):
    runner = make_runner(tmp_path, monkeypatch)
    (tmp_path / 'SRR1.sra.part').write_bytes(b'NCBI.sra1234')
    runner._run = Mock(side_effect=ToolError('bad checksum'))
    with pytest.raises(ToolError, match='bad checksum'):
        runner._prefetch_public_s3('SRR1', tmp_path, 'GSM1', logging.getLogger())
    assert (tmp_path / 'SRR1.sra.part').exists()
    assert not (tmp_path / 'SRR1.sra').exists()


def test_configured_proxy_preserves_tls_verification(tmp_path, monkeypatch):
    runner=make_runner(tmp_path,monkeypatch)
    runner.cfg.data['docker']['download_proxy']='http://127.0.0.1:7890'
    head=Mock(return_value=subprocess.CompletedProcess([],0,'Content-Length: 12\r\n',''))
    monkeypatch.setattr('scpre.docker_run.subprocess.run',head)
    (tmp_path/'SRR1.sra.part').write_bytes(b'NCBI.sra1234')
    runner._run=Mock()
    runner._prefetch_public_s3('SRR1',tmp_path,'GSM1',logging.getLogger())
    argv=head.call_args.args[0]
    assert argv[1:3]==['--proxy','http://127.0.0.1:7890']
    assert '--insecure' not in argv and '-k' not in argv


@pytest.mark.parametrize('transport_code', [35, 28, 56])
def test_transport_failure_resumes_and_validates(tmp_path, monkeypatch, transport_code):
    runner=make_runner(tmp_path,monkeypatch)
    runner._shutdown.wait=Mock(return_value=False)
    part=tmp_path/'SRR1.sra.part'
    seen=[]
    def start(argv,**kwargs):
        assert '--retry' not in argv
        assert argv[argv.index('--continue-at')+1] == '-'
        seen.append(part.read_bytes() if part.exists() else b'')
        proc=Mock()
        if len(seen)==1:
            part.write_bytes(b'NCBI.sra')
            proc.returncode=transport_code
            proc.communicate.return_value=('TLS handshake failed',None)
        else:
            part.write_bytes(b'NCBI.sra1234')
            proc.returncode=0
            proc.communicate.return_value=('',None)
        return proc
    monkeypatch.setattr('scpre.docker_run.subprocess.Popen',start)
    runner._run=Mock()
    result=runner._prefetch_public_s3('SRR1',tmp_path,'GSM1',logging.getLogger())
    assert seen==[b'',b'NCBI.sra']
    assert result.read_bytes()==b'NCBI.sra1234'
    runner._run.assert_called_once()


def test_certificate_verification_failure_is_not_retried(tmp_path, monkeypatch):
    runner=make_runner(tmp_path,monkeypatch)
    proc=Mock(returncode=60)
    proc.communicate.return_value=('certificate verification failed',None)
    start=Mock(return_value=proc)
    monkeypatch.setattr('scpre.docker_run.subprocess.Popen',start)
    runner._run=Mock()
    with pytest.raises(ToolError,match='certificate verification failed'):
        runner._prefetch_public_s3('SRR1',tmp_path,'GSM1',logging.getLogger())
    start.assert_called_once()
    runner._run.assert_not_called()


def test_transient_head_handshake_failure_retries(tmp_path, monkeypatch):
    runner=make_runner(tmp_path,monkeypatch)
    runner._shutdown.wait=Mock(return_value=False)
    head=Mock(side_effect=[subprocess.CompletedProcess([],35,'','handshake failed'),
                          subprocess.CompletedProcess([],0,'Content-Length: 12\r\n','')])
    monkeypatch.setattr('scpre.docker_run.subprocess.run',head)
    (tmp_path/'SRR1.sra.part').write_bytes(b'NCBI.sra1234')
    runner._run=Mock()
    runner._prefetch_public_s3('SRR1',tmp_path,'GSM1',logging.getLogger())
    assert head.call_count==2
