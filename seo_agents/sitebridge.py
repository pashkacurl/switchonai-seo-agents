"""Transactional site adapter. Release commands are locally configured by the owner."""
from __future__ import annotations
import argparse
import contextlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import urllib.request
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser
from .content import Page
from .core import Stop, digest, now, read_json, safe_slug, validate_url, within, write_json
from .network import public_url, Redirects


@contextlib.contextmanager
def file_lock(path):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('a+b') as f:
        if os.name=='nt':
            import msvcrt
            f.seek(0);f.write(b'0');f.flush();f.seek(0)
            try:msvcrt.locking(f.fileno(),msvcrt.LK_NBLCK,1)
            except OSError as exc:raise Stop('На сервере сайта уже выполняется публикация.') from exc
            try:yield
            finally:f.seek(0);msvcrt.locking(f.fileno(),msvcrt.LK_UNLCK,1)
        else:
            import fcntl
            try:fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except OSError as exc:raise Stop('На сервере сайта уже выполняется публикация.') from exc
            try:yield
            finally:fcntl.flock(f,fcntl.LOCK_UN)


def fetch_public(url):
    public_url(url)
    req=urllib.request.Request(url,headers={'User-Agent':'SwitchOnAI-SEO-Release/0.1','Cache-Control':'no-cache'})
    try:
        with urllib.request.build_opener(Redirects()).open(req,timeout=30) as response:
            raw=response.read(4_000_001)
            if len(raw)>4_000_000:raise Stop('Слишком большой ответ сайта.')
            return {'status':response.status,'url':response.url,'text':raw.decode('utf-8',errors='replace'),
                    'headers':dict(response.headers)}
    except (OSError,ValueError) as exc:
        raise Stop('Не удалось проверить опубликованный URL.') from None


def verify_release(config,bundle,fetch=fetch_public):
    expected=config['site_url'].rstrip('/')+config['blog_path']+bundle['slug']+'/'
    if bundle['url']!=expected:raise Stop('URL операции не соответствует профилю сайта.')
    response=fetch(expected);page=Page(response['url']);page.feed(response['text'])
    if response['status']!=200 or response['url'].rstrip('/')!=expected.rstrip('/'):
        raise Stop('Конечный URL или HTTP-статус публикации не соответствует ожиданию.')
    if page.canonical.rstrip('/')!=expected.rstrip('/') or 'noindex' in page.robots.lower():
        raise Stop('Проверьте canonical и noindex опубликованной страницы.')
    if any('noindex' in v.lower() for k,v in response.get('headers',{}).items() if k.lower()=='x-robots-tag'):
        raise Stop('Опубликованный URL закрыт X-Robots-Tag.')
    if bundle['title'] not in page.title or not page.h1:
        raise Stop('Опубликованная страница не содержит ожидаемый заголовок.')
    class Revision(Page):
        def __init__(self,url):super().__init__(url);self.revision=''
        def handle_starttag(self,tag,attrs):
            super().handle_starttag(tag,attrs);a=dict(attrs)
            if tag=='meta' and a.get('name')=='seo-revision':self.revision=a.get('content','')
    check=Revision(expected);check.feed(response['text'])
    if check.revision!=bundle['revision']:
        raise Stop('На сайте ещё нет ожидаемой seo-revision; успех не подтверждён.')
    base=config['site_url'].rstrip('/')
    robots=fetch(base+'/robots.txt');rp=RobotFileParser();rp.parse(robots['text'].splitlines())
    if not rp.can_fetch('Googlebot',expected):raise Stop('robots.txt запрещает обход опубликованной страницы.')
    from .monitoring import sitemap_urls
    class Reader:
        def request(self,url):return fetch(url)
    sitemap=sitemap_urls(Reader(),base+'/sitemap.xml',10000)
    if expected.rstrip('/') not in {u.rstrip('/') for u in sitemap}:
        raise Stop('URL отсутствует в sitemap.')
    for filename in bundle['media']:
        if fetch(base+'/media/seo/'+filename)['status']!=200:raise Stop('Медиа недоступно.')
    return {'status':200,'canonical':page.canonical,'revision':check.revision,'verified_at':now()}


class Site:
    def __init__(self,filename,runner=None,fetch=None):
        self.config=read_json(filename);c=self.config
        required=('checkout','content_dir','public_dir','state_dir','site_url','blog_path',
                  'build_command','deploy_command','rollback_command','timeout_seconds','acceptance_command')
        if any(k not in c for k in required):raise Stop('Неполный site-profile.json; используйте config/site-profile.example.json.')
        self.checkout=Path(c['checkout']).resolve();self.state=Path(c['state_dir']).resolve()
        if not self.checkout.is_dir():raise Stop('Папка сайта не существует.')
        if self.state==self.checkout or self.state.is_relative_to(self.checkout):
            raise Stop('state_dir сайта должен находиться вне рабочего checkout.')
        self.state.mkdir(parents=True,exist_ok=True)
        validate_url(c['site_url'])
        if not c['blog_path'].startswith('/') or not c['blog_path'].endswith('/') or '..' in c['blog_path']:
            raise Stop('Неверный blog_path.')
        for name in ('build_command','deploy_command','rollback_command','acceptance_command'):
            if not isinstance(c[name],list) or not c[name] or not all(isinstance(x,str) and x for x in c[name]):
                raise Stop('В профиле сайта требуется массив аргументов '+name+'.')
        if type(c['timeout_seconds']) is not int or not 1<=c['timeout_seconds']<=1800:
            raise Stop('timeout_seconds: 1–1800.')
        within(self.checkout,c['content_dir']+'/probe.mdx');within(self.checkout,c['public_dir']+'/probe.svg')
        self.runner=runner or subprocess.run;self.fetch=fetch or fetch_public

    def command(self,name,operation):
        env=os.environ.copy();env['SEO_OPERATION_ID']=operation
        # No model text or shell substitution reaches the command line.
        try:
            r=self.runner(self.config[name],cwd=self.checkout,env=env,capture_output=True,
                          text=True,encoding='utf-8',timeout=self.config['timeout_seconds'])
        except subprocess.TimeoutExpired as exc:raise Stop('Превышено время команды '+name+'. Результат требует сверки.') from exc
        except OSError as exc:raise Stop('Не удалось выполнить настроенную команду '+name+'.') from exc
        if r.returncode:raise Stop('Команда '+name+' завершилась ошибкой. Подробности проверьте в локальном журнале сайта.')

    def paths(self,bundle):
        safe_slug(bundle['slug'])
        result={'article.mdx':within(self.checkout,self.config['content_dir']+'/'+bundle['slug']+'.mdx')}
        for filename in bundle['media']:
            if not re.fullmatch(re.escape(bundle['slug'])+r'-[1-9][0-9]*\.svg',filename):raise Stop('Неверное имя медиа.')
            result[filename]=within(self.checkout,self.config['public_dir']+'/media/seo/'+filename)
        return result

    def restore(self,receipt):
        paths=self.paths(receipt['bundle'])
        # Check every file before touching any: never erase subsequent owner changes.
        for key,old in receipt['backups'].items():
            path=paths[key]
            current=path.read_bytes() if path.exists() else None
            old_hash=digest(old.encode()) if old is not None else None
            new_hash=receipt['bundle']['content_sha256'] if key=='article.mdx' else receipt['bundle']['media_sha256'][key]
            if (digest(current) if current is not None else None) not in (old_hash,new_hash):
                raise Stop('Файл изменён после выпуска; автоматический откат остановлен до ручной сверки.')
        for key,old in receipt['backups'].items():
            path=paths[key]
            if old is None:path.unlink(missing_ok=True)
            else:path.write_bytes(old.encode('utf-8'))

    def commit(self,paths,operation):
        if not (self.checkout/'.git').exists():return ''
        relative=[str(p.relative_to(self.checkout)) for p in paths.values()]
        for argv in (['git','add','-A','--',*relative],['git','commit','-m','seo: release '+operation]):
            r=self.runner(argv,cwd=self.checkout,capture_output=True,text=True,encoding='utf-8')
            if r.returncode:raise Stop('Не удалось сохранить файлы выпуска в Git; проверьте локальную историю сайта.')
        r=self.runner(['git','rev-parse','HEAD'],cwd=self.checkout,capture_output=True,text=True,encoding='utf-8')
        if r.returncode:raise Stop('Не удалось прочитать коммит выпуска.')
        return r.stdout.strip()

    def dispatch(self,request):
        action=request.get('action');operation=request.get('operation_id','')
        if not re.fullmatch('[a-f0-9]{40}',operation):raise Stop('Некорректный operation_id.')
        receipt_path=within(self.state,operation+'.json')
        with file_lock(self.state/'publish.lock'):
            old=read_json(receipt_path) if receipt_path.exists() else None
            if action=='status':
                if old and old['status'] in ('deploying','uncertain'):
                    try:
                        checks=verify_release(self.config,old['bundle'],self.fetch)
                        old.update(status='accepted',checks=checks,accepted_at=now());write_json(receipt_path,old)
                    except Stop:pass
                return self.public_receipt(old) if old else {'status':'not_started','operation_id':operation}
            if action=='rollback':
                if not old:raise Stop('Операция не найдена.')
                if old['status']=='rolled_back':return self.public_receipt(old)
                self.restore(old)
                if old.get('source_commit'):self.commit(self.paths(old['bundle']),operation+'-rollback')
                self.command('rollback_command',operation)
                old.update(status='rolled_back',updated_at=now());write_json(receipt_path,old)
                return self.public_receipt(old)
            if action!='publish':raise Stop('Неизвестное действие site bridge.')
            if old:return self.public_receipt(old)  # Never re-execute an uncertain deployment.
            bundle=request.get('bundle',{});files=request.get('files',{})
            if bundle.get('operation_id')!=operation:raise Stop('Операция и bundle не совпадают.')
            if not isinstance(files,dict) or any(not isinstance(v,str) for v in files.values()):raise Stop('Некорректные файлы.')
            if sum(len(v) for v in files.values())>1_000_000:raise Stop('Пакет статьи слишком большой.')
            paths=self.paths(bundle)
            if set(files)!=set(paths):raise Stop('Набор файлов не соответствует bundle.')
            if digest(files['article.mdx'].encode())!=bundle['content_sha256']:raise Stop('Хеш статьи не совпал.')
            parts=files['article.mdx'].split('\n---\n',1)
            if not files['article.mdx'].startswith('---\n') or len(parts)!=2:raise Stop('Не найден frontmatter статьи.')
            if re.search(r'<|[{}]|^\s*(?:import|export)\s',parts[1],re.M):
                raise Stop('В теле статьи обнаружен исполняемый MDX/HTML.')
            front={}
            try:
                for line in parts[0].splitlines()[1:]:
                    key,value=line.split(': ',1)
                    if key in front:raise ValueError('duplicate')
                    front[key]=json.loads(value)
            except (ValueError,TypeError):raise Stop('Frontmatter должен содержать уникальные поля с JSON-значениями.')
            expected_fields={'slug','title','description','date','lastmod','author','category','draft','cluster_id','page_id','seo_revision'}
            if set(front)!=expected_fields or front['draft'] is not False or any(not isinstance(v,str) for k,v in front.items() if k!='draft'):
                raise Stop('Frontmatter не соответствует контракту статьи.')
            if any(front[k]!=bundle[b] for k,b in [('slug','slug'),('title','title'),('seo_revision','revision'),('page_id','page_id'),('cluster_id','cluster_id')]):
                raise Stop('Frontmatter не соответствует bundle.')
            if not re.fullmatch('[a-f0-9]{64}',bundle.get('revision','')):raise Stop('Некорректная revision.')
            for key in bundle['media']:
                if digest(files[key].encode())!=bundle['media_sha256'].get(key):raise Stop('Хеш медиа не совпал.')
                # Only renderer-generated static SVG is accepted, never executable markup.
                if re.search(r'<(?:script|foreignObject)|\bon\w+\s*=|(?:href|src)\s*=',files[key],re.I):raise Stop('Небезопасное SVG.')
            article=paths['article.mdx']
            if bundle['kind']=='new' and article.exists():raise Stop('Целевой файл уже существует; новая статья его не перезаписывает.')
            if bundle['kind']=='refresh' and (not article.exists() or digest(article.read_bytes())!=bundle.get('previous_sha256')):
                raise Stop('Версия обновляемой статьи изменилась; требуется повторное согласование.')
            if bundle['kind'] not in ('new','refresh'):raise Stop('Неверный тип выпуска.')
            # Site repository must be clean before the package writes selected files.
            if (self.checkout/'.git').exists():
                status=self.runner(['git','status','--porcelain'],cwd=self.checkout,capture_output=True,text=True,encoding='utf-8')
                if status.returncode or status.stdout.strip():raise Stop('В checkout сайта есть изменения; выпуск остановлен.')
            old={'operation_id':operation,'status':'prepared','bundle':bundle,'created_at':now(),
                 'backups':{k:p.read_bytes().decode('utf-8') if p.exists() else None for k,p in paths.items()}}
            write_json(receipt_path,old)
            try:
                for key,path in paths.items():
                    path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(files[key].encode('utf-8'))
                old['status']='written';write_json(receipt_path,old)
                self.command('build_command',operation)
                self.command('acceptance_command',operation)
                old['source_commit']=self.commit(paths,operation)
                old['status']='deploying';write_json(receipt_path,old)
                self.command('deploy_command',operation)
                checks=verify_release(self.config,bundle,self.fetch)
                old.update(status='accepted',checks=checks,accepted_at=now())
            except Stop as exc:
                if old['status']=='deploying':
                    old.update(status='uncertain',reason=str(exc))
                else:
                    self.restore(old);old.update(status='failed',reason=str(exc))
                write_json(receipt_path,old)
                raise
            write_json(receipt_path,old)
            return self.public_receipt(old)

    @staticmethod
    def public_receipt(receipt):
        return {k:v for k,v in receipt.items() if k!='backups'}


def main():
    p=argparse.ArgumentParser(description='Одноразовый приёмник публикации на сервере сайта.')
    p.add_argument('--config',required=True);args=p.parse_args()
    try:
        raw=sys.stdin.read(1_200_001)
        if len(raw)>1_200_000:raise Stop('Запрос слишком большой.')
        result=Site(args.config).dispatch(json.loads(raw))
        print(json.dumps(result,ensure_ascii=False));return 0
    except (Stop,ValueError,KeyError,TypeError) as exc:
        print(json.dumps({'status':'error','error':str(exc) if isinstance(exc,Stop) else 'Некорректный запрос.'},ensure_ascii=False));return 2
