"""Build an ECHO import ZIP. Python stdlib only; no private data inputs."""
from pathlib import Path
from zipfile import ZipFile,ZIP_DEFLATED
import json,hashlib
ROOT=Path(__file__).resolve().parents[1]
CONTENT=ROOT/'workshop/content';(CONTENT/'assets').mkdir(parents=True,exist_ok=True)
DIST=ROOT/'dist';DIST.mkdir(exist_ok=True)
def digest(b):return hashlib.sha256(b).hexdigest()
bundle=CONTENT/'assets/bridge.data'
with ZipFile(bundle,'w',ZIP_DEFLATED) as z:
 for p in sorted((ROOT/'bridge/app').rglob('*.py')):z.write(p,p.relative_to(ROOT/'bridge').as_posix())
 for name in ['setup_runtime.py','requirements.txt']:z.write(ROOT/'bridge'/name,name)
 for name in ['LICENSE','DISCLAIMER.md','LICENSE-REVIEW.md']:z.write(ROOT/name,name)
 if (ROOT/'requirements-lock.txt').exists():z.write(ROOT/'requirements-lock.txt','constraints.txt')
package=json.loads((ROOT/'workshop/package-template.json').read_text(encoding='utf-8'))
package['files']=[]
for p in sorted((ROOT/'workshop/src').iterdir()):
 if p.suffix not in ['.js','.mjs','.html','.css','.json']:continue
 text=p.read_text(encoding='utf-8-sig').replace('__BUNDLE_SHA__',digest(bundle.read_bytes()))
 package['files'].append({'path':p.name,'content':text})
raw=json.dumps(package,ensure_ascii=False,indent=2).encode();assert len(raw)<2097152
(CONTENT/'community.echo').write_bytes(raw)
manifest=json.loads((ROOT/'workshop/item-template.json').read_text(encoding='utf-8'))
manifest['files']=[{'path':p.relative_to(CONTENT).as_posix(),'size':len(p.read_bytes()),'sha256':digest(p.read_bytes())} for p in [CONTENT/'community.echo',bundle]]
(CONTENT/'echo.workshop.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
target=DIST/'Afterecho-0.2.0.zip'
with ZipFile(target,'w',ZIP_DEFLATED) as z:
 for p in [CONTENT/'echo.workshop.json',CONTENT/'community.echo',bundle]:z.write(p,p.relative_to(CONTENT).as_posix())
with ZipFile(target) as z:
 assert z.testzip() is None
 for entry in manifest['files']:
  b=z.read(entry['path']);assert len(b)==entry['size'] and digest(b)==entry['sha256']
print(f'{target.name}: {target.stat().st_size} bytes; hashes and CRC verified')

