"""Create one source ZIP and SHA256SUMS in the package's single dist directory."""
import hashlib
from pathlib import Path
import subprocess
import sys
import zipfile
ROOT=Path(__file__).resolve().parents[1]
subprocess.run([sys.executable,'-B',str(ROOT/'scripts/check_package.py')],check=True,cwd=ROOT)
subprocess.run([sys.executable,'-B','-m','unittest','discover','-s','tests','-q'],check=True,cwd=ROOT)
sys.path.insert(0,str(ROOT))
from seo_agents import __version__
dist=ROOT/'dist';dist.mkdir(exist_ok=True)
target=dist/('switchonai-seo-agents-'+__version__+'.zip')
with zipfile.ZipFile(target,'w',zipfile.ZIP_DEFLATED) as z:
    for p in sorted(ROOT.rglob('*')):
        rel=p.relative_to(ROOT)
        if not p.is_file() or p.is_symlink() or set(rel.parts)&{'.git','dist','__pycache__'}:continue
        info=zipfile.ZipInfo('switchonai-seo-agents/'+rel.as_posix(),date_time=(2026,11,6,0,0,0))
        info.compress_type=zipfile.ZIP_DEFLATED;info.external_attr=0o100644<<16
        z.writestr(info,p.read_bytes())
sha=hashlib.sha256(target.read_bytes()).hexdigest()
(dist/'SHA256SUMS').write_text(sha+'  '+target.name+'\n',encoding='utf-8')
print(str(target));print(sha)
