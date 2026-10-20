"""Read-only structural audit inside the velocyto image; see deployment guide."""
import gzip, json, subprocess, sys
from pathlib import Path
import h5py

folders = sorted(p for p in Path('/archive').iterdir() if p.is_dir())
if len(sys.argv) > 1:
    missing = set(sys.argv[1:]) - {p.name for p in folders}
    if missing:
        raise SystemExit('Missing samples: ' + ', '.join(sorted(missing)))
failed = False
audited = 0
for folder in folders:
    if len(sys.argv) > 1 and folder.name not in sys.argv[1:]:
        continue
    audited += 1
    row = {'sample': folder.name, 'errors': []}
    outs = folder/'results/outs'
    check = subprocess.run(['samtools','quickcheck','-v',str(outs/'possorted_genome_bam.bam')], capture_output=True, text=True)
    if check.returncode: row['errors'].append(check.stdout+check.stderr)
    for name in ['filtered_feature_bc_matrix.h5','raw_feature_bc_matrix.h5','molecule_info.h5']:
        try:
            with h5py.File(outs/name, 'r') as f:
                if 'matrix' in f:
                    shape = f['matrix/shape'][:].tolist()
                    row[name] = shape
                    assert len(f['matrix/indptr']) == shape[1]+1
                    assert len(f['matrix/data']) == len(f['matrix/indices'])
                else:
                    assert name == 'molecule_info.h5'
                    assert all(k in f for k in ['barcodes', 'barcode_idx', 'feature_idx', 'count', 'umi'])
                    n = len(f['count'])
                    assert all(len(f[k]) == n for k in ['barcode_idx', 'feature_idx', 'umi'])
                    row['molecule_rows'] = n
        except Exception as e: row['errors'].append(name+': '+str(e))
    try:
        bam = outs/'possorted_genome_bam.bam'
        assert (outs/'possorted_genome_bam.bam.bai').read_bytes()[:4] == b'BAI\x01'
        header = subprocess.run(['samtools', 'view', '-H', str(bam)],
                                capture_output=True, text=True, check=True)
        first_ref = next(field[3:] for line in header.stdout.splitlines()
                         if line.startswith('@SQ\t') for field in line.split('\t')
                         if field.startswith('SN:'))
        indexed = subprocess.run(['samtools', 'view', '-c', str(bam), first_ref+':1-1'],
                                 capture_output=True, text=True, check=True)
        assert indexed.stdout.strip().isdigit()
        row['bam_index_query_passed'] = True
    except Exception as e: row['errors'].append('BAM index: '+str(e))
    for kind in ['filtered', 'raw']:
        try:
            mtx_dir = outs/(kind+'_feature_bc_matrix')
            with gzip.open(mtx_dir/'matrix.mtx.gz', 'rt') as f:
                dims = next(line for line in f if not line.startswith('%'))
            genes, cells, nnz = map(int, dims.split())
            assert [genes, cells] == row[kind+'_feature_bc_matrix.h5']
            with gzip.open(mtx_dir/'barcodes.tsv.gz', 'rt') as f:
                assert sum(1 for _ in f) == cells
            with gzip.open(mtx_dir/'features.tsv.gz', 'rt') as f:
                assert sum(1 for _ in f) == genes
            row[kind+'_mtx_dimensions'] = [genes, cells, nnz]
        except Exception as e: row['errors'].append(kind+' MTX: '+str(e))
    try:
        with h5py.File(folder/'loom'/(folder.name+'.loom'),'r') as f:
            shape = f['matrix'].shape; row['loom_shape'] = shape
            assert shape[1] == row['filtered_feature_bc_matrix.h5'][1]
            assert len(f['col_attrs/CellID']) == shape[1]
            for layer in ['spliced','unspliced','ambiguous']:
                assert f['layers/'+layer].shape == shape
                f['layers/'+layer][0:2,0:2]
    except Exception as e: row['errors'].append('loom: '+str(e))
    print(json.dumps(row), flush=True)
    failed = failed or bool(row['errors'])
if not audited:
    raise SystemExit('No samples were audited')
raise SystemExit(1 if failed else 0)
