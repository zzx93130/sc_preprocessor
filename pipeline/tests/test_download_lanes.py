"""Independent download/whole-preprocessing scheduling invariants."""
import threading
import time

from test_pipeline import make_env, drain


def test_separate_lanes_complete_full_verified_mock_pipeline(tmp_path):
    cfg, state, q = make_env(tmp_path)
    cfg.data['concurrency'].update(separate_downloads=True,
                                   max_preprocessing_samples=1,
                                   max_download_jobs=1, download_ahead=1)
    q.scan_inbox()
    samples = drain(q, state)
    assert all(s['status'] == 'done' for s in samples)
    for sample in samples:
        events = state.events_for(sample['id'])
        assert sum(e['stage'] == 'downloading' for e in events) == 1
        assert all(e['status'] == 'completed' for e in events)


def test_download_overlaps_only_one_whole_preprocessing_job(tmp_path, monkeypatch):
    cfg, state, q = make_env(tmp_path)
    cfg.data['concurrency'].update(separate_downloads=True,
                                   max_preprocessing_samples=1,
                                   max_download_jobs=1, download_ahead=1)
    q.scan_inbox()
    # Add enough work to expose accidental over-scheduling.
    base = state.list_samples()[0]
    for sid in ('READY_A', 'READY_B', 'MISSING_A', 'MISSING_B'):
        state.upsert_sample(sid, project=cfg.project, mode='sra',
                            srrs=base['srrs'], status='queued', stage='queued',
                            meta=base['meta'])
    ready = {'READY_A', 'READY_B'}
    monkeypatch.setattr(q, '_download_ready', lambda s: s['id'] in ready)
    # Pre-existing downloaded backlog blocks further download-ahead.
    release = threading.Event()
    started = []

    def worker(sid, lane=None):
        started.append((sid, lane))
        release.wait(5)

    monkeypatch.setattr(q, '_worker', worker)
    try:
        q.tick()
        assert len(q.workers) == 1
        assert set(q._worker_lanes.values()) == {'preprocess'}
        # Remove the second ready item to allow a background download.
        other = next(s for s in ready if s not in q.workers)
        state.upsert_sample(other, status='done', stage='done')
        q.tick()
        assert sorted(q._worker_lanes.values()) == ['download', 'preprocess']
        q.tick()
        assert len(q.workers) == 2
        assert sum(v == 'preprocess' for v in q._worker_lanes.values()) == 1
    finally:
        release.set()
        for thread in q.workers.values():
            thread.join(5)


def test_download_lane_yields_before_conversion(tmp_path):
    cfg, state, q = make_env(tmp_path)
    q.scan_inbox()
    sid = state.list_samples()[0]['id']
    q._worker(sid, 'download')
    sample = state.get_sample(sid)
    assert sample['status'] == 'queued'
    assert sample['stage'] == 'converting'
    assert q._download_ready(sample)
    assert [e['stage'] for e in state.events_for(sid)] == ['downloading']


def test_operator_pause_also_stops_both_lanes(tmp_path):
    cfg, state, q = make_env(tmp_path)
    cfg.data['concurrency']['separate_downloads'] = True
    q.scan_inbox()
    q.pause('paused from dashboard')
    q.tick()
    assert not q.workers
