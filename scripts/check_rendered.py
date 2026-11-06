"""Acceptance hook for statically exported HTML; never guesses the built output path."""
import argparse
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from seo_agents.content import Page

p=argparse.ArgumentParser();p.add_argument('--html-dir',required=True);a=p.parse_args()
root=Path(a.html_dir)
if not root.is_dir():raise SystemExit('Build HTML directory does not exist; configure adapter for your build type.')
files=list(root.rglob('*.html'));count=0
for file in files:
    text=file.read_text(encoding='utf-8')
    if 'seo-revision' not in text:continue
    page=Page('https://example.com/');page.feed(text)
    if len(page.h1)!=1 or not page.title or not page.description or not page.canonical or 'noindex' in page.robots:
        raise SystemExit('Article HTML failed acceptance: '+str(file.relative_to(root)))
    if any(not image['alt'].strip() for image in page.images):raise SystemExit('Image without alt: '+str(file.relative_to(root)))
    count+=1
if not count:raise SystemExit('No rendered articles with seo-revision. Select correct output or implement SSR acceptance hook.')
print('Rendered article checks:',count)
