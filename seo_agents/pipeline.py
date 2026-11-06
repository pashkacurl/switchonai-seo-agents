"""Durable article stages and publisher transport; no success before a site receipt."""
from __future__ import annotations
import datetime as dt
import json
from pathlib import Path
import subprocess
from .content import check_article, materialize, research
from .core import Stop, digest, now, read_json, write_json
from .sitebridge import Site, verify_release


def fresh_research(project,topic_id):
    data=read_json(project.draft_dir(topic_id)/'research.json')
    try:age=(dt.datetime.now(dt.timezone.utc)-dt.datetime.fromisoformat(data['at'])).days
    except (ValueError,TypeError) as exc:raise Stop('Некорректная дата исследования.') from exc
    if age<0 or age>project.config['limits']['source_age_days']:
        raise Stop('Исследование устарело; обновите источники до нового текста.')
    approved=set(project.config['sources']['approved_urls'])
    if any(s['url'] not in approved or s['final_url'] not in approved for s in data['sources']):
        raise Stop('Согласование источников изменилось; проверьте исследование.')
    return data


def draft(project,topic_id,model):
    row=project.topic(topic_id)
    if row['status'] not in ('researched','drafting','blocked'):
        raise Stop('Сначала выполните research; готовые статьи команда draft не перезаписывает.')
    data=fresh_research(project,topic_id)
    from .core import within
    business=within(project.root,project.config['site']['business_file']).read_text(encoding='utf-8')
    if 'Укажите реальные продукт' in business or len(business.strip())<80:
        raise Stop('Заполните business.md проверенной фактурой бизнеса.')
    directory=project.draft_dir(topic_id)
    fingerprint=digest({'research':data,'business':business,'site':project.config['site'],
                        'model':project.config['model']})
    stamp=directory/'context.json'
    if stamp.exists() and read_json(stamp)['sha256']!=fingerprint:
        raise Stop('Входы изменились после начала генерации. Архивируйте старые этапы командой restart-draft.')
    write_json(stamp,{'sha256':fingerprint})
    project.update_topic(topic_id,'drafting')
    def stage(name,role,payload):
        path=directory/(name+'.json')
        if path.exists():return read_json(path)
        value=model.generate(role,payload);write_json(path,value);return value
    try:
        brief=stage('brief','brief',{'research':data,'business':business,'site':project.config['site']})
        if brief.get('stop_reasons'):raise Stop('Бриф требует дополнительной фактуры: изучите brief.json.')
        article=stage('author','author',{'brief':brief,'research':data,'business':business,'site':project.config['site']})
        check_article(article,data,project)
        accepted=False
        for attempt in range(project.config['publish']['max_review_attempts']):
            review=stage('editor-'+str(attempt+1),'editor',{'article':article,'brief':brief,'research':data,'business':business})
            article=check_article(review['article'],data,project)
            if review['verdict']=='block':raise Stop('Редактор заблокировал статью: требуется разбор сохранённых замечаний.')
            if review['verdict']=='accept' and not review['issues']:
                accepted=True;break
        if not accepted:raise Stop('Редактор не принял статью в пределах лимита доработок.')
        write_json(directory/'article.json',article)
        bundle=materialize(project,topic_id,article)
        project.update_topic(topic_id,'review')
        return bundle
    except Stop as exc:
        project.update_topic(topic_id,'blocked',str(exc));raise


def approve(project,topic_id):
    row=project.topic(topic_id)
    if row['status']!='review':raise Stop('На согласование можно передать только готовую проверенную статью.')
    fresh_research(project,topic_id)
    directory=project.draft_dir(topic_id);bundle=read_json(directory/'bundle.json')
    if digest((directory/'article.mdx').read_bytes())!=bundle['content_sha256']:
        raise Stop('Текст изменён после проверок. Пройдите редактуру повторно.')
    write_json(directory/'approval.json',{'bundle_sha256':digest(bundle),'approved_at':now(),'method':'owner_cli'})
    project.update_topic(topic_id,'approved')
    return bundle


class Publisher:
    def __init__(self,project,http,runner=None,site=None):
        self.project,self.http=project,http;self.runner=runner or subprocess.run;self.site=site

    def rpc(self,request):
        cfg=self.project.config['publish']
        if cfg['adapter']=='local':
            site=self.site or Site(cfg['site_config'])
            return site.dispatch(request)
        if cfg['adapter']!='ssh' or not cfg['ssh_command']:
            raise Stop('Настройте publish.adapter и site bridge по docs/site-adapter.md.')
        if not isinstance(cfg['ssh_command'],list) or not all(isinstance(x,str) for x in cfg['ssh_command']):
            raise Stop('ssh_command должен быть массивом аргументов без shell.')
        try:
            r=self.runner(cfg['ssh_command'],input=json.dumps(request,ensure_ascii=False),
                capture_output=True,text=True,encoding='utf-8',timeout=1800)
        except (OSError,subprocess.TimeoutExpired) as exc:
            raise Stop('Ответ сайта не получен. Сначала publish-status, не повторная публикация.') from exc
        try:response=json.loads(r.stdout)
        except ValueError as exc:raise Stop('Site bridge не вернул JSON-квитанцию; результат неопределён.') from exc
        if r.returncode or response.get('status')=='error':raise Stop('Site bridge сообщил об ошибке; проверьте локальный статус операции.')
        return response

    def publish(self,topic_id,allow_publish=False):
        if not self.http.live or not allow_publish:
            raise Stop('Публикация требует согласованного --live --allow-publish.')
        row=self.project.topic(topic_id);directory=self.project.draft_dir(topic_id)
        bundle=read_json(directory/'bundle.json')
        approval=read_json(directory/'approval.json')
        if approval['bundle_sha256']!=digest(bundle):raise Stop('Bundle изменён после согласования.')
        if row['status'] not in ('approved','publishing','published'):
            raise Stop('Статья не согласована для публикации.')
        fresh_research(self.project,topic_id)
        files={'article.mdx':(directory/'article.mdx').read_text(encoding='utf-8')}
        files.update({name:(directory/name).read_text(encoding='utf-8') for name in bundle['media']})
        if digest(files['article.mdx'].encode())!=bundle['content_sha256']:raise Stop('Хеш текста изменился после согласования.')
        for name in bundle['media']:
            if digest(files[name].encode())!=bundle['media_sha256'][name]:raise Stop('Хеш медиа изменился после согласования.')
        if row['status']=='approved':
            self.project.reserve('updates' if row['kind']=='refresh' else 'articles')
            self.project.update_topic(topic_id,'publishing')
        existing=self.rpc({'action':'status','operation_id':bundle['operation_id']})
        if existing['status']=='not_started':
            result=self.rpc({'action':'publish','operation_id':bundle['operation_id'],'bundle':bundle,'files':files})
        else:result=existing
        return self.accept(topic_id,bundle,result)

    def accept(self,topic_id,bundle,result):
        directory=self.project.draft_dir(topic_id)
        write_json(directory/'site-receipt.json',result)
        if result.get('status')!='accepted':
            raise Stop('Публикация не подтверждена; статус сайта: '+str(result.get('status'))+'.')
        if result.get('operation_id')!=bundle['operation_id'] or result.get('bundle',{}).get('content_sha256')!=bundle['content_sha256']:
            raise Stop('Квитанция относится к другой операции или версии статьи.')
        # Controller independently checks the actual public release using its request budget.
        config={'site_url':self.project.config['site']['url'],'blog_path':self.project.config['site']['blog_path']}
        checks=verify_release(config,bundle,lambda url:self.http.request(url))
        write_json(directory/'public-check.json',checks)
        self.project.update_topic(topic_id,'published')
        from .delivery import enqueue_publication
        enqueue_publication(self.project,bundle)
        return result

    def status(self,topic_id):
        bundle=read_json(self.project.draft_dir(topic_id)/'bundle.json')
        result=self.rpc({'action':'status','operation_id':bundle['operation_id']})
        write_json(self.project.draft_dir(topic_id)/'site-receipt.json',result)
        if result.get('status')=='accepted' and self.http.live:
            return self.accept(topic_id,bundle,result)
        return result


def run_one(project,http,model,publisher):
    cfg=project.config
    if cfg['publish']['mode']!='automatic' or not cfg['publish']['verified_trial']:
        raise Stop('Автоматический режим включается после принятой пробной статьи через activate.')
    trial=project.topic(cfg['publish']['verified_trial'])
    if trial['status']!='published':raise Stop('Пробная публикация не принята.')
    # Reconcile an outstanding publication before selecting another topic.
    pending=project.db.execute('SELECT id FROM topics WHERE status="publishing" ORDER BY updated LIMIT 1').fetchone()
    if pending:return publisher.publish(pending[0],allow_publish=True)
    available=[]
    for kind,budget in [('new','articles'),('refresh','updates')]:
        used=project.db.execute('SELECT used FROM budgets WHERE day=? AND kind=?',(project.day(),budget)).fetchone()
        if cfg['limits'][budget+'_per_day']>(used[0] if used else 0):available.append(kind)
    if not available:raise Stop('Дневной лимит новых статей и обновлений исчерпан; генерация не начата.')
    placeholders=','.join('?' for _ in available)
    row=project.db.execute('SELECT id,status FROM topics WHERE status IN ("queued","researched","review","approved") AND kind IN ('+placeholders+') ORDER BY updated LIMIT 1',available).fetchone()
    if not row:raise Stop('Нет готовых тем. Проверьте candidates в отчёте и импортируйте подтверждённые кластеры.')
    topic_id=row['id']
    if row['status']=='queued':research(project,topic_id,http)
    if project.topic(topic_id)['status']=='researched':draft(project,topic_id,model)
    if project.topic(topic_id)['status']=='review':
        bundle=read_json(project.draft_dir(topic_id)/'bundle.json')
        write_json(project.draft_dir(topic_id)/'approval.json',{'bundle_sha256':digest(bundle),'approved_at':now(),
                    'method':'configured_automatic_policy','trial':cfg['publish']['verified_trial']})
        project.update_topic(topic_id,'approved')
    return publisher.publish(topic_id,allow_publish=True)
