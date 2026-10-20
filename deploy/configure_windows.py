"""Create a fresh Windows configuration; never overwrite an existing one."""
import argparse
import os
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--data-root', type=Path, required=True)
    parser.add_argument('--reference', type=Path, required=True)
    parser.add_argument('--cores', type=int, default=10)
    parser.add_argument('--memory-gb', type=int, default=36)
    args = parser.parse_args()
    args.root = args.root.resolve()
    args.data_root = args.data_root.resolve()
    if args.cores < 1 or args.memory_gb < 16:
        parser.error('Invalid resource budget')
    sys.path.insert(0, str(args.root / 'pipeline'))
    from scpre.config import Config
    import yaml
    target = args.root / 'config.yaml'
    if target.exists():
        parser.error('config.yaml already exists; refusing overwrite')
    runtime = Path(os.environ['LOCALAPPDATA']) / 'scpre'
    if (runtime / 'state.db').exists():
        parser.error('An existing scpre database exists; use its deployment, not a second scheduler')
    host_metadata = args.data_root / 'tools' / 'cellranger-10.1.0'
    cfg = Config({
        'project': 'default',
        'paths': {
            'cellranger_dir': str(host_metadata),
            'reference_dir': str(args.reference.resolve()),
            'work_root': str(args.data_root.resolve()),
            'archive_root': str(args.data_root / 'archive'),
            'inbox_dir': str(args.data_root / 'to_process'),
            'state_db': str(runtime / 'state.db'),
            'logs_dir': str(runtime / 'logs'),
        },
        'concurrency': {
            'separate_downloads': True, 'max_download_jobs': 1,
            'max_preprocessing_samples': 1, 'download_ahead': 1,
            'max_cellranger_jobs': 1, 'cellranger_cores': args.cores,
            'cellranger_mem_gb': args.memory_gb, 'convert_threads': 4,
        },
        'docker': {'cellranger_volume': 'scpre-cellranger-10.1.0',
                   'cellranger_work_volume_prefix': 'scpre-pipestance'},
        'cleanup': {'remove_inbox': False, 'restart_docker_desktop_at_idle': False},
        'pipeline': {'create_bam': True, 'make_loom': True, 'chemistry': 'auto'},
        'metadata': {'sra_run_table': str(args.data_root / 'to_process' / 'SraRunTable.csv'),
                     'process_all': True, 'read_maps': {}},
    }, args.root)
    for folder in [args.data_root / 'archive', args.data_root / 'to_process',
                   args.data_root / 'work', host_metadata, runtime / 'logs',
                   args.root / 'runs' / 'reports']:
        folder.mkdir(parents=True, exist_ok=True)
    # Export only the read-role whitelist and version metadata. Executable Unix
    # symlinks remain on the Docker Linux volume, never on an exFAT host path.
    import subprocess
    barcode_dir = host_metadata / 'lib/python/cellranger/barcodes'
    barcode_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(['docker.exe', 'run', '--rm',
                    '--mount', 'type=volume,source=scpre-cellranger-10.1.0,target=/opt/cellranger,readonly',
                    '--mount', f'type=bind,source={barcode_dir},target=/export',
                    'scpre-crbase:latest', 'sh', '-ec',
                    'cp /opt/cellranger/lib/python/cellranger/barcodes/737K-august-2016.txt /export/'], check=True)
    (host_metadata / '.version').write_text('10.1.0\n')
    with target.open('x', encoding='utf-8') as handle:
        yaml.safe_dump(cfg.data, handle, sort_keys=False)
    print(target)


if __name__ == '__main__':
    main()
