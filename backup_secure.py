"""Authenticated encrypted snapshots. Key is supplied via DUES_BACKUP_KEY only."""
import os,io,json,tempfile,zipfile
from pathlib import Path
from cryptography.fernet import Fernet
import maintenance

def create(source,destination,key):
    destination=Path(destination)
    if destination.exists():raise ValueError('Choose a new backup file.')
    cipher=Fernet(key)
    with tempfile.TemporaryDirectory(prefix='dues-backup-') as temp:
        folder=Path(temp)/'snapshot';maintenance.backup(source,folder)
        if sum(p.stat().st_size for p in folder.iterdir())>256*1024*1024:raise ValueError('Backup exceeds the 256 MB local snapshot limit.')
        stream=io.BytesIO()
        with zipfile.ZipFile(stream,'w',compression=zipfile.ZIP_DEFLATED) as z:
            for p in folder.iterdir():z.write(p,p.name)
        raw=cipher.encrypt(stream.getvalue())
        fd=os.open(destination,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'wb') as f:f.write(raw)
    return destination

def restore(source,destination,key):
    source=Path(source)
    if source.stat().st_size>270*1024*1024:raise ValueError('Backup too large.')
    raw=Fernet(key).decrypt(source.read_bytes())
    with tempfile.TemporaryDirectory(prefix='dues-restore-') as temp:
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            names=z.namelist()
            if len(names)!=len(set(names)) or sum(x.file_size for x in z.infolist())>256*1024*1024:raise ValueError('Invalid archive size.')
            for name in names:
                if name!='manifest.json' and not maintenance.NAME.fullmatch(name):raise ValueError('Invalid archive entry.')
                (Path(temp)/name).write_bytes(z.read(name))
        return maintenance.restore(temp,destination)

if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser();p.add_argument('action',choices=['create','restore']);p.add_argument('source');p.add_argument('destination');a=p.parse_args()
    if not os.getenv('DUES_BACKUP_KEY'):p.error('Set DUES_BACKUP_KEY privately; never pass it as a command argument.')
    globals()[a.action](a.source,a.destination,os.environ['DUES_BACKUP_KEY'].encode())
    print('Encrypted backup operation complete.')
