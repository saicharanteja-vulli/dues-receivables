"""Offline backup/restore. Never put backup folders in a public repository."""
import argparse,hashlib,json,os,re,sqlite3
from pathlib import Path
NAME=re.compile(r'(accounts|workspace-[0-9a-f]{32}|dues)\.db$')
def backup(source,destination):
    source=Path(source);destination=Path(destination)
    if not source.is_dir():raise ValueError('Data directory does not exist.')
    if destination.exists():raise ValueError('Choose a new backup folder.')
    files=list(source.glob('*.db'))
    if not files:raise ValueError('No database files found.')
    destination.mkdir(parents=True,mode=0o700);manifest={}
    for file in files:
        if not NAME.fullmatch(file.name):continue
        out=destination/file.name
        with sqlite3.connect(file.resolve().as_uri()+'?mode=ro',uri=True) as src,sqlite3.connect(out) as dst:src.backup(dst)
        out.chmod(0o600);manifest[file.name]=hashlib.sha256(out.read_bytes()).hexdigest()
    (destination/'manifest.json').write_text(json.dumps(manifest,indent=2))
    (destination/'manifest.json').chmod(0o600)
    return len(manifest)
def restore(source,destination):
    source=Path(source);destination=Path(destination)
    if destination.exists():raise ValueError('Restore only into a new directory; existing records are never overwritten.')
    manifest=json.loads((source/'manifest.json').read_text())
    if 'accounts.db' not in manifest:raise ValueError('Full backup must contain accounts.db.')
    for name,digest in manifest.items():
        if not NAME.fullmatch(name):raise ValueError('Unexpected database name.')
        file=source/name
        if file.is_symlink() or hashlib.sha256(file.read_bytes()).hexdigest()!=digest:raise ValueError('Backup checksum mismatch.')
        with sqlite3.connect(file.resolve().as_uri()+'?mode=ro',uri=True) as c:
            if c.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('Invalid database.')
    destination.mkdir(parents=True,mode=0o700)
    for name in manifest:
        target=destination/name;target.write_bytes((source/name).read_bytes());target.chmod(0o600)
    return len(manifest)
if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('action',choices=['backup','restore']);parser.add_argument('source');parser.add_argument('destination');args=parser.parse_args()
    os.umask(0o077)
    try:print(f'{globals()[args.action](args.source,args.destination)} databases {args.action} complete.')
    except (ValueError,OSError,sqlite3.Error) as e:parser.exit(1,str(e)+'\n')
