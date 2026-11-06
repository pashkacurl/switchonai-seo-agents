"""One-shot scheduler. Never installs or starts a service automatically."""
import datetime as dt
import json
import shlex
import sys
from pathlib import Path
from .core import Stop, now, read_json, write_json


def activate(project,trial='',reports=False,content=False):
    c=project.config
    if not reports and not content:raise Stop('Выберите --reports и/или --content.')
    if reports:
        last=read_json(project.state_dir/'last-report.json')
        if not last['technical_ok']:raise Stop('Сначала получите успешный реальный технический отчёт.')
    if content:
        if not trial or project.topic(trial)['status']!='published':raise Stop('Нужна принятая пробная статья --trial TOPIC.')
        if not c['limits']['articles_per_day'] or not c['limits']['model_calls_per_day'] or not c['limits']['output_tokens_per_day']:
            raise Stop('Согласуйте дневные лимиты выпуска и модели.')
        check=read_json(project.draft_dir(trial)/'public-check.json')
        if check.get('status')!=200:raise Stop('Нет подтверждения живого URL пробной статьи.')
        expected=c['site']['url'].rstrip('/')+c['site']['blog_path']+project.topic(trial)['slug']+'/'
        if check.get('canonical','').rstrip('/')!=expected.rstrip('/'):
            raise Stop('Пробная публикация относится к другому профилю сайта.')
        c['publish'].update(mode='automatic',verified_trial=trial)
    c['schedule'].update(enabled=True,reports_enabled=reports,content_enabled=content)
    write_json(project.root/'profile.json',c);project.event('activated',data={'reports':reports,'content':content,'trial':trial})
    return {'enabled':True,'reports':reports,'content':content,'note':'Расписание ещё нужно добавить в ОС командой из schedule.'}


def pause(project):
    project.config['schedule']['enabled']=False
    write_json(project.root/'profile.json',project.config)
    return {'enabled':False,'note':'Следующий tick ничего не запускает. Уже начатый выпуск завершается; PID не убивается. Проверьте status.'}


def schedule_command(project):
    command=[sys.executable,'-B',str(Path(__file__).resolve().parents[1]/'seo.py'),'--project',str(project.root),
             'tick','--live','--allow-publish']
    return {'linux_cron':'*/15 * * * * '+shlex.join(command),
            'windows_argv':command,'installed':False,
            'note':'Linux: добавьте ровно одну строку crontab выделенного пользователя, сохранив чужие задания. Windows: Task Scheduler каждые 15 минут. Хост должен быть включён; время выбирается из profile.json.'}


def tick(project,http,model,publisher,allow_publish=False,clock=None):
    if not http.live:raise Stop('Рабочий tick требует --live.')
    c=project.config;s=c['schedule']
    if not s['enabled']:return {'status':'paused'}
    instant=clock or dt.datetime.now(project.tz);day=instant.date().isoformat();result={}
    from .monitoring import collect
    from .delivery import enqueue_text,deliver
    from .pipeline import run_one
    report_key='report-'+day
    if s['reports_enabled'] and instant.hour>=s['report_hour'] and not project.db.execute('SELECT id FROM runs WHERE id=?',(report_key,)).fetchone():
        # Reservation survives uncertain runs; restarting does not re-spend the same run.
        with project.db:project.db.execute('INSERT INTO runs VALUES(?,?,?,?)',(report_key,now(),'started','{}'))
        ident,report=collect(project,http,weekly=instant.weekday()==s['weekly_day'],model=model if s['strategy_enabled'] else None)
        enqueue_text(project,(project.root/'reports'/(ident+'.md')).read_text(encoding='utf-8'),ident)
        with project.db:project.db.execute('UPDATE runs SET status="done",payload=? WHERE id=?',(json.dumps({'report':ident}),report_key))
        result['report']=ident
    if s['content_enabled'] and instant.hour>=s['content_start_hour']:
        if not allow_publish:raise Stop('Автовыпуск требует --allow-publish.')
        # Re-read pause flag between report and publication; do not use stale config.
        if read_json(project.root/'profile.json')['schedule']['enabled']:
            try:result['publication']=run_one(project,http,model,publisher)
            except Stop as exc:
                result['publication']={'status':'stopped','reason':str(exc)}
                enqueue_text(project,'Выпуск приостановлен: '+str(exc),['content-stop',day,str(exc)])
    result['deliveries']=deliver(project,http)
    return result
