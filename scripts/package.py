#!/usr/bin/env python3
"""Create a source baseline archive with an explicit allowlist."""
from datetime import datetime
import hashlib
import json
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[1]
FILES = ['.gitignore','README.md','index.html','server.py','seed.json',
         'scripts/package.py','tests/test_server.py','tests/verify-browser.cjs']


def build():
    out = ROOT / 'dist'
    out.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    target = out / f'deli-assign-dashboard-baseline-{timestamp}.zip'
    manifest = {'createdAt':datetime.now().astimezone().isoformat(),
                'contents':'source and seed data only; no database, sessions, runtime or credentials',
                'files':{name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in FILES}}
    with zipfile.ZipFile(target,'w',compression=zipfile.ZIP_DEFLATED) as bundle:
        for name in FILES:
            bundle.write(ROOT/name, f'deli-assign-dashboard/{name}')
        bundle.writestr('deli-assign-dashboard/manifest.json',json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    target.with_suffix('.zip.sha256').write_text(f'{digest}  {target.name}\n')
    print(target)
    return target


if __name__ == '__main__':
    build()
