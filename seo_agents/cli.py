"""A small explicit CLI shared by interactive setup and cron."""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import zipfile
from .core import Project,Stop,initialize,read_json,write_json,validate_url,now,process_exists


def doctor(project):
    c=project.config;issues=[]
    if c['site']['url']=='https://example.com':issues.append('Замените пример site.url своим доменом.')
    for field in ('author','language','market','region'):
        if not c['site'][field]:issues.append('Заполните site.'+field+'.')
    if not c['model']['name']:issues.append('Выберите доступную модель model.name.')
    if c['model']['provider']=='codex' and not shutil.which(c['model']['codex_executable']):issues.append('Codex CLI не найден; установку/авторизацию выполняет владелец.')
    if not c['limits']['requests_per_day']:issues.append('Сетевой бюджет равен нулю; live-запросы заблокированы.')
    if c['publish']['adapter']=='local':
        if not c['publish']['site_config']:issues.append('Не настроен site bridge; отчёты доступны независимо от публикации.')
        else:
            from .sitebridge import Site
            try:Site(c['publish']['site_config'])
            except Stop as exc:issues.append(str(exc))
    elif c['publish']['adapter']!='ssh' or not c['publish']['ssh_command']:issues.append('Не настроен транспорт site bridge.')
    return {'status':'needs_setup' if issues else 'configured_not_live_verified','issues':issues,
            'python':sys.version.split()[0],'secrets':{k:bool(v) for k,v in project.secrets.items()},
            'schedule':c['schedule'],'limits':c['limits'],'network_requests':0,
            'note':'Наличие ключа не подтверждает доступ. Проверка доступа — отдельный --live запуск report/research/draft в согласованных лимитах.'}


def backup(project,path):
    path=Path(path).resolve()
    if path.exists():raise Stop('Файл резервной копии уже существует.')
    if path.is_relative_to(project.root):raise Stop('Сохраняйте резерв вне каталога состояния.')
    with tempfile.TemporaryDirectory(prefix='seo-backup-') as temporary:
        db_path=Path(temporary)/'state.sqlite3'
        target=sqlite3.connect(db_path)
        try:project.db.backup(target)
        finally:target.close()
        with zipfile.ZipFile(path,'x',zipfile.ZIP_DEFLATED) as z:
            z.write(db_path,'state/state.sqlite3')
            for file in project.root.rglob('*'):
                rel=file.relative_to(project.root)
                if file.is_file() and not file.is_symlink() and file.name!='.env' and not file.name.startswith('state.sqlite3'):
                    z.write(file,rel.as_posix())
            z.writestr('BACKUP-NOTE.txt','Private profile backup. Secrets excluded. Site bridge receipts and website require separate backup.\n')
        os.chmod(path,0o600)
    return {'path':str(path),'secrets_included':False,'note':'Копия содержит частную фактуру и статьи. Не размещайте её в публичном репозитории.'}


def main(argv=None):
    p=argparse.ArgumentParser(description='Switch On AI: SEO-отчёты и последовательный выпуск статей.')
    p.add_argument('--project',help='Явная папка личного профиля, не каталог публичного пакета.')
    sub=p.add_subparsers(dest='command',required=True)
    for name in ('init','doctor','status','pause','schedule','demo'):sub.add_parser(name)
    q=sub.add_parser('import-topics');q.add_argument('csv')
    q=sub.add_parser('sources-approve');q.add_argument('urls',nargs='+')
    q=sub.add_parser('topic-verify');q.add_argument('topic');g=q.add_mutually_exclusive_group(required=True);g.add_argument('--serp-run-id');g.add_argument('--decision')
    for name in ('research','draft','approve','publish','publish-status','rollback','restart-draft'):
        q=sub.add_parser(name);q.add_argument('topic')
        if name not in ('approve','restart-draft'):q.add_argument('--live',action='store_true')
        if name in ('publish','rollback'):q.add_argument('--allow-publish',action='store_true')
    q=sub.add_parser('report');q.add_argument('--live',action='store_true');q.add_argument('--weekly',action='store_true');q.add_argument('--strategy',action='store_true')
    q=sub.add_parser('deliver');q.add_argument('--live',action='store_true');q.add_argument('--retry-unknown',action='store_true')
    for name in ('run-once','tick'):
        q=sub.add_parser(name);q.add_argument('--live',action='store_true');q.add_argument('--allow-publish',action='store_true')
    q=sub.add_parser('activate');q.add_argument('--trial',default='');q.add_argument('--reports',action='store_true');q.add_argument('--content',action='store_true')
    q=sub.add_parser('unlock');q.add_argument('--pid',required=True,type=int)
    q=sub.add_parser('backup');q.add_argument('output')
    args=p.parse_args(argv);project=None
    try:
        if sys.version_info<(3,11):raise Stop('Нужен Python 3.11 или новее.')
        if args.command!='demo' and not args.project:raise Stop('Укажите --project с явным путём личного профиля.')
        if args.command=='init':result={'profile':str(initialize(args.project))}
        elif args.command=='demo':
            from .demo import demo
            result=demo()
        else:
            project=Project(args.project)
            from .network import HTTP
            from .models import Model
            from .pipeline import Publisher,approve,draft,run_one
            from .content import import_topics,research
            from .scheduler import activate,pause,schedule_command,tick
            from .delivery import deliver,enqueue_text
            http=HTTP(project,live=getattr(args,'live',False));model=Model(project,http);publisher=Publisher(project,http)
            if args.command=='doctor':result=doctor(project)
            elif args.command=='status':
                result={'schedule':project.config['schedule'],'topics':[dict(x) for x in project.db.execute('SELECT id,slug,kind,status,updated,error FROM topics')],
                        'budgets':[dict(x) for x in project.db.execute('SELECT * FROM budgets WHERE day=?',(project.day(),))],
                        'deliveries':[dict(x) for x in project.db.execute('SELECT id,kind,status,attempts,error FROM deliveries WHERE status!="sent"')],
                        'locks':[dict(x) for x in project.db.execute('SELECT name,pid FROM locks')]}
            elif args.command=='pause':result=pause(project)
            elif args.command=='schedule':result=schedule_command(project)
            elif args.command=='unlock':
                locks=project.db.execute('SELECT * FROM locks WHERE pid=?',(args.pid,)).fetchall()
                if not locks:raise Stop('Блокировка с таким PID не найдена.')
                if process_exists(args.pid):raise Stop('Процесс существует; unlock запрещён. Процесс не остановлен.')
                with project.db:project.db.execute('DELETE FROM locks WHERE pid=?',(args.pid,))
                result={'unlocked_pid':args.pid}
            else:
                with project.lock():
                    name=args.command
                    if name=='import-topics':result={'imported':import_topics(project,args.csv)}
                    elif name=='sources-approve':
                        approved=project.config['sources']['approved_urls']
                        for url in args.urls:
                            validate_url(url)
                            if url not in approved:approved.append(url)
                        write_json(project.root/'profile.json',project.config);result={'approved':len(approved)}
                    elif name=='topic-verify':
                        row=project.topic(args.topic)
                        if row['status']!='needs_research':raise Stop('Проверять можно только тему needs_research.')
                        payload=json.loads(row['payload'])
                        payload.update(evidence_status='confirmed' if args.serp_run_id else 'waiver',serp_run_id=args.serp_run_id or '',owner_decision=args.decision or '')
                        with project.db:project.db.execute('UPDATE topics SET payload=? WHERE id=?',(json.dumps(payload,ensure_ascii=False),args.topic))
                        project.update_topic(args.topic,'queued');result={'status':'queued','evidence_status':payload['evidence_status']}
                    elif name=='research':result=research(project,args.topic,http)
                    elif name=='draft':result=draft(project,args.topic,model)
                    elif name=='approve':result=approve(project,args.topic)
                    elif name=='publish':result=publisher.publish(args.topic,args.allow_publish)
                    elif name=='publish-status':
                        if not args.live:raise Stop('Проверка сайта требует --live.')
                        result=publisher.status(args.topic)
                    elif name=='rollback':
                        if not args.live or not args.allow_publish:raise Stop('Откат требует --live --allow-publish.')
                        bundle=read_json(project.draft_dir(args.topic)/'bundle.json')
                        result=publisher.rpc({'action':'rollback','operation_id':bundle['operation_id']})
                        if result['status']=='rolled_back':project.update_topic(args.topic,'rolled_back')
                    elif name=='restart-draft':
                        row=project.topic(args.topic)
                        if row['status'] in ('publishing','published'):raise Stop('Сначала выясните результат публикации; текст не меняется.')
                        directory=project.draft_dir(args.topic)
                        if directory.exists():
                            archive=project.root/'drafts-archive';archive.mkdir(exist_ok=True)
                            directory.rename(archive/(args.topic+'-'+dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%f')))
                        project.update_topic(args.topic,'queued');result={'status':'queued','note':'Предыдущие стадии архивированы; повтор модели расходует новый бюджет.'}
                    elif name=='report':
                        if not args.live:raise Stop('Для реального отчёта нужен --live; без сети используйте demo.')
                        from .monitoring import collect
                        ident,report=collect(project,http,args.weekly,model if args.strategy else None)
                        enqueue_text(project,(project.root/'reports'/(ident+'.md')).read_text(encoding='utf-8'),ident)
                        result={'report_id':ident,'files':str(project.root/'reports'),'modules':{k:v['status'] for k,v in report['modules'].items()}}
                    elif name=='deliver':result=deliver(project,http,args.retry_unknown)
                    elif name=='activate':result=activate(project,args.trial,args.reports,args.content)
                    elif name=='tick':result=tick(project,http,model,publisher,args.allow_publish)
                    elif name=='run-once':
                        if not args.live or not args.allow_publish:raise Stop('run-once требует --live --allow-publish.')
                        result=run_one(project,http,model,publisher)
                    elif name=='backup':result=backup(project,args.output)
                    else:raise Stop('Неизвестная команда.')
        print(json.dumps(result,ensure_ascii=False,indent=2));return 0
    except (Stop,OSError,ValueError,KeyError,TypeError,sqlite3.Error) as exc:
        print(json.dumps({'status':'error','message':str(exc) if isinstance(exc,Stop) else 'Ошибка файла, параметров или состояния. Проверьте профиль и status; значения секретов скрыты.'},ensure_ascii=False),file=sys.stderr)
        return 2
    finally:
        if project:project.close()
