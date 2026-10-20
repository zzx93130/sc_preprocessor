"""Portable configuration generation with no real Docker or service writes."""
import importlib.util
from pathlib import Path
import sys

import pytest
import yaml


def load_generator():
    path = Path(__file__).resolve().parents[2] / 'deploy/configure_windows.py'
    spec = importlib.util.spec_from_file_location('windows_config', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_windows_policy_and_no_overwrite(tmp_path, monkeypatch):
    root = tmp_path / 'repo'
    root.mkdir()
    data = tmp_path / 'data'
    runtime = tmp_path / 'ntfs'
    monkeypatch.setenv('LOCALAPPDATA', str(runtime))
    calls = []
    monkeypatch.setattr('subprocess.run', lambda *a, **kw: calls.append((a, kw)))
    monkeypatch.setattr(sys, 'argv', ['configure_windows', '--root', str(root),
                                    '--data-root', str(data), '--reference', str(tmp_path / 'reference')])
    module = load_generator()
    module.main()
    config = root / 'config.yaml'
    before = config.read_bytes()
    cfg = yaml.safe_load(before)
    assert cfg['concurrency']['separate_downloads'] is True
    assert cfg['concurrency']['max_preprocessing_samples'] == 1
    assert cfg['concurrency']['max_download_jobs'] == 1
    assert cfg['concurrency']['download_ahead'] == 1
    assert cfg['cleanup']['remove_inbox'] is False
    assert cfg['cleanup']['restart_docker_desktop_at_idle'] is False
    assert cfg['pipeline']['create_bam'] and cfg['pipeline']['make_loom']
    assert cfg['paths']['state_db'] == str(runtime / 'scpre/state.db')
    assert cfg['docker']['cellranger_work_volume_prefix']
    assert cfg['metadata']['read_maps'] == {}
    assert 'paired_only_runs' not in cfg['metadata']
    assert len(calls) == 1 and calls[0][1]['check'] is True
    with pytest.raises(SystemExit):
        module.main()
    assert config.read_bytes() == before and len(calls) == 1


def test_existing_database_blocks_second_deployment(tmp_path, monkeypatch):
    runtime = tmp_path / 'local/scpre'
    runtime.mkdir(parents=True)
    (runtime / 'state.db').write_bytes(b'existing')
    monkeypatch.setenv('LOCALAPPDATA', str(runtime.parent))
    monkeypatch.setattr(sys, 'argv', ['configure_windows', '--root', str(tmp_path / 'repo'),
                                    '--data-root', str(tmp_path / 'data'), '--reference', str(tmp_path / 'ref')])
    with pytest.raises(SystemExit):
        load_generator().main()
    assert not (tmp_path / 'repo/config.yaml').exists()
    assert (runtime / 'state.db').read_bytes() == b'existing'
