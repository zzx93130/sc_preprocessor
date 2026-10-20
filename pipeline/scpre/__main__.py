"""scpre CLI: init | start | dry-run | add-sample | scan | status | retry"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import config as cfgmod
from .util import app_logger

log = app_logger()


def cmd_init(args):
    root = cfgmod.find_root(Path(args.root) if args.root else None)
    cfgmod.init_wizard(root, non_interactive=args.yes,
                       docker_check=not args.no_docker_check)


def _load(args):
    from .queue import Queue
    from .state import State
    cfg = _load_config(args)
    state = State(cfg.state_db)
    q = Queue(cfg, state)
    return cfg, state, q


def _load_config(args):
    if getattr(args, "config", None):
        return cfgmod.load_file(
            Path(args.config), Path(args.root) if args.root else None
        )
    return cfgmod.load(Path(args.root) if args.root else None)


def _load_cluster(args):
    from .manager import ClusterManager
    from .state import State
    cfg = _load_config(args)
    if not cfg.get("cluster", "enabled", default=False):
        raise SystemExit("cluster.enabled is false in the selected configuration")
    state = State(cfg.state_db)
    manager = ClusterManager(cfg, state)
    return cfg, state, manager


def _acquire_service_lock(cfg):
    """Prevent a second scheduler from touching the same state/work tree."""
    cfg.state_db.parent.mkdir(parents=True, exist_ok=True)
    path = cfg.state_db.parent / "service.lock"
    handle = open(path, "a+")
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(" ")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        owner = "unknown"
        try:
            handle.seek(0)
            owner = handle.read().strip() or owner
        except OSError:
            pass
        handle.close()
        raise SystemExit(
            f"scpre is already running for {cfg.root} (owner PID {owner})")
    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()))
    handle.flush()
    return handle


def cmd_start(args):
    from .server import serve
    cfg, state, q = _load(args)
    service_lock = _acquire_service_lock(cfg)
    n = q.scan_inbox()
    log.info("startup scan registered %d new sample(s)", n)
    try:
        serve(cfg, state, q, q.runner)
    finally:
        service_lock.close()


def cmd_dry_run(args):
    from .dryrun import run_dry_run
    sys.exit(run_dry_run(keep=args.keep, base_dir=args.dir))


def cmd_scan(args):
    _cfg, _state, q = _load(args)
    print(f"registered {q.scan_inbox()} new sample(s)")


def cmd_add_sample(args):
    cfg, _state, q = _load(args)
    paths = []
    for pat in args.fastq:
        p = Path(pat)
        if p.is_dir():
            paths += sorted(p.glob("*.fastq.gz"))
        else:
            paths += sorted(Path().glob(pat)) or ([p] if p.exists() else [])
    paths = [p for p in paths if p.is_file()]
    if not paths:
        sys.exit(f"no FASTQ files matched: {args.fastq}")
    q.add_fastq_sample(args.sample, paths)
    print(f"added {args.sample} with {len(paths)} FASTQ file(s); "
          "it will start on the next queue tick (service must be running)")


def cmd_status(args):
    _cfg, state, _q = _load(args)
    samples = state.list_samples()
    if not samples:
        print("no samples registered")
        return
    w = max(len(s["id"]) for s in samples) + 2
    for s in samples:
        err = f"  ! {s['error'][:80]}" if s["error"] else ""
        print(f"{s['id']:<{w}} {s['status']:<8} {s['stage']:<12} "
              f"runs={len(s['srrs'])}{err}")


def cmd_retry(args):
    _cfg, _state, q = _load(args)
    err = q.retry(args.sample)
    if err:
        sys.exit(err)
    print(f"{args.sample} requeued (service picks it up on next tick)")


def cmd_cluster_start(args):
    from .server import serve
    cfg, state, manager = _load_cluster(args)
    service_lock = _acquire_service_lock(cfg)
    try:
        added = manager.scan_inbox()
        log.info("cluster startup scan registered %d new sample(s)", added)
        serve(cfg, state, manager, manager)
    finally:
        service_lock.close()


def cmd_cluster_status(args):
    _cfg, state, manager = _load_cluster(args)
    manager.reconcile()
    for sample in state.list_samples():
        meta = sample["meta"]
        print(
            f"{sample['id']:<16} {sample['status']:<8} "
            f"{sample['stage']:<18} job={meta.get('pbs_job_id', '-')} "
            f"node={meta.get('compute_node', '-')}"
        )


def cmd_cluster_retry(args):
    _cfg, _state, manager = _load_cluster(args)
    error = manager.retry(args.sample)
    if error:
        raise SystemExit(error)
    print(f"{args.sample} queued for PBS retry")


def cmd_cluster_add_fastq(args):
    _cfg, _state, manager = _load_cluster(args)
    paths = []
    for value in args.fastq:
        path = Path(value)
        if path.is_dir():
            paths.extend(sorted(path.glob("*.fastq.gz")))
        elif path.is_file():
            paths.append(path)
    if not paths:
        raise SystemExit("no FASTQ files matched")
    manager.add_fastq_sample(args.sample, paths)
    print(f"staged {len(paths)} FASTQ files for {args.sample}")


def cmd_cluster_cancel(args):
    _cfg, _state, manager = _load_cluster(args)
    error = manager.cancel(args.sample)
    if error:
        raise SystemExit(error)
    print(f"{args.sample} PBS job cancelled; scratch was preserved")


def cmd_cluster_preflight(args):
    from .cluster_preflight import run_preflight
    cfg = _load_config(args)
    checks = run_preflight(cfg, args.role)
    for check in checks:
        mark = "OK" if check["ok"] else "FAIL"
        print(f"[{mark}] {check['name']}: {check['detail']}")
    if not all(check["ok"] for check in checks):
        raise SystemExit(1)


def cmd_cluster_artifacts(args):
    from .artifact_store import write_catalog
    catalog = write_catalog(Path(args.output), {
        "cellranger": Path(args.cellranger),
        "reference": Path(args.reference),
        "sratools": Path(args.sratools_sif),
        "crbase": Path(args.crbase_sif),
        "velocyto": Path(args.velocyto_sif),
    })
    print(
        f"wrote {args.output} with "
        f"{len(catalog['artifacts'])} verified artifacts"
    )


def cmd_cluster_migration_plan(args):
    from .migration import build_plan
    plan = build_plan(
        Path(args.source_db), Path(args.source_archive),
        Path(args.source_inbox), Path(args.output),
    )
    summary = plan["summary"]
    print(
        f"migration plan: {summary['total']} samples, "
        f"{summary['completed_archives']} completed archives, "
        f"{summary['requeue']} to requeue "
        f"({summary['requeue_with_all_inputs']} with all inputs present)"
    )


def main():
    ap = argparse.ArgumentParser(prog="scpre",
                                 description="Local monitored single-cell preprocessing")
    ap.add_argument("--root", help="project root (default: auto-detect)")
    ap.add_argument("--config", help="explicit YAML configuration file")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="interactive first-run configuration")
    p.add_argument("--yes", action="store_true",
                   help="non-interactive: accept detected defaults")
    p.add_argument("--no-docker-check", action="store_true")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("start", help="run queue + dashboard (foreground)")
    p.set_defaults(func=cmd_start)

    p = sub.add_parser("dry-run", help="validate pipeline end-to-end with mock tools")
    p.add_argument("--keep", action="store_true", help="keep the temp project")
    p.add_argument("--dir", help="use this directory instead of a temp one")
    p.set_defaults(func=cmd_dry_run)

    p = sub.add_parser("scan", help="rescan inbox for new samples")
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("add-sample", help="register a FASTQ-mode sample")
    p.add_argument("--sample", required=True, help="sample name")
    p.add_argument("--fastq", nargs="+", required=True,
                   help="FASTQ files, globs, or a directory")
    p.set_defaults(func=cmd_add_sample)

    p = sub.add_parser("status", help="print sample table")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("retry", help="clean + requeue a failed sample")
    p.add_argument("sample")
    p.set_defaults(func=cmd_retry)

    p = sub.add_parser(
        "cluster-start", help="run the PBS manager and cluster dashboard"
    )
    p.set_defaults(func=cmd_cluster_start)

    p = sub.add_parser(
        "cluster-status", help="reconcile PBS and print cluster sample status"
    )
    p.set_defaults(func=cmd_cluster_status)

    p = sub.add_parser(
        "cluster-retry", help="retry a failed sample, preferring its old node"
    )
    p.add_argument("sample")
    p.set_defaults(func=cmd_cluster_retry)

    p = sub.add_parser(
        "cluster-add-fastq",
        help="checksum and stage a FASTQ sample into the cluster inbox",
    )
    p.add_argument("--sample", required=True)
    p.add_argument("--fastq", nargs="+", required=True)
    p.set_defaults(func=cmd_cluster_add_fastq)

    p = sub.add_parser(
        "cluster-cancel", help="explicitly cancel a sample's PBS job"
    )
    p.add_argument("sample")
    p.set_defaults(func=cmd_cluster_cancel)

    p = sub.add_parser(
        "cluster-preflight", help="validate manager or worker deployment"
    )
    p.add_argument("--role", choices=("manager", "worker"), required=True)
    p.set_defaults(func=cmd_cluster_preflight)

    p = sub.add_parser(
        "cluster-artifacts", help="write a checksummed shared artifact catalog"
    )
    p.add_argument("--output", required=True)
    p.add_argument("--cellranger", required=True)
    p.add_argument("--reference", required=True)
    p.add_argument("--sratools-sif", required=True)
    p.add_argument("--crbase-sif", required=True)
    p.add_argument("--velocyto-sif", required=True)
    p.set_defaults(func=cmd_cluster_artifacts)

    p = sub.add_parser(
        "cluster-migration-plan",
        help="inventory an existing queue without modifying it",
    )
    p.add_argument("--source-db", required=True)
    p.add_argument("--source-archive", required=True)
    p.add_argument("--source-inbox", required=True)
    p.add_argument("--output", required=True)
    p.set_defaults(func=cmd_cluster_migration_plan)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
