"""Dependency-free release gate: source syntax, contracts, privacy and documentation."""
import ast
import json
from pathlib import Path
import re
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from seo_agents.core import default_config
from seo_agents.models import schema_for,validate

files=[p for p in ROOT.rglob('*') if p.is_file() and not set(p.relative_to(ROOT).parts)&{'.git','dist','__pycache__'}]
errors=[]
for p in files:
    rel=p.relative_to(ROOT)
    if p.suffix.lower() in ('.sqlite3','.pdf','.tar','.gz') or p.name=='.env':errors.append(str(rel)+': private/runtime file')
    if p.suffix.lower() not in ('.py','.md','.json','.csv','.txt') and p.name not in ('LICENSE','.gitignore','.env.example'):continue
    text=p.read_text(encoding='utf-8-sig')
    if p.suffix=='.py':ast.parse(text,filename=str(rel))
    if p.suffix=='.json':json.loads(text)
    # Pattern literals in this checker are split to avoid matching themselves.
    if re.search(r'[A-Za-z]:[/\\]'+r'Users[/\\]|/Users/[A-Za-z]|switchonai'+r'\.ru',text):errors.append(str(rel)+': personal context')
    if re.search(r'sk-'+'[A-Za-z0-9_-]{25,}|ghp_'+'[A-Za-z0-9]{25,}|-----BEGIN '+'(?:RSA |OPENSSH )?PRIVATE KEY',text):errors.append(str(rel)+': possible secret')
    if p.suffix=='.md':
        for url in re.findall(r'\]\(([^)]+)\)',text):
            if '://' not in url and not url.startswith('#') and not (p.parent/url.split('#')[0]).exists():errors.append(str(rel)+': broken link '+url)
for name in ('brief','author','editor','strategist'):
    schema=schema_for(name)
    assert schema['type']=='object' and schema['additionalProperties'] is False
    assert (ROOT/'prompts'/(name+'.md')).stat().st_size>500
example=json.loads((ROOT/'config/profile.example.json').read_text(encoding='utf-8'))
assert example==default_config(),'Profile example drift'
assert example['schedule']['enabled'] is False and example['publish']['mode']=='review'
assert all(example['limits'][name]==0 for name in ('requests_per_day','model_calls_per_day','articles_per_day','updates_per_day','output_tokens_per_day'))
assert 'SEO-factory starter contributors' in (ROOT/'NOTICE.md').read_text(encoding='utf-8')
assert 'Pavel Leshchenko / Switch On AI' in (ROOT/'LICENSE').read_text(encoding='utf-8')
if errors:raise SystemExit('\n'.join(errors))
print(json.dumps({'status':'passed','files_checked':len(files),'syntax_contracts_links_privacy':True,'disabled_defaults':True}))
