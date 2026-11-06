"""Durable, explicitly requested notifications; publishing success never depends on delivery."""
import json
from urllib.parse import quote
from .core import Stop, digest, now, validate_url
from .providers import google_token


def enqueue(project,kind,payload,identity):
    ident=digest([kind,identity])[:40]
    with project.db:
        project.db.execute('INSERT OR IGNORE INTO deliveries(id,kind,payload,status) VALUES(?,?,?,?)',
                           (ident,kind,json.dumps(payload,ensure_ascii=False),'pending'))
    return ident


def enqueue_text(project,text,identity):
    if not project.config['notifications']['telegram_chat_id']:return []
    # Plain text avoids HTML/Markdown injection; acknowledge each part separately.
    chunks=[text[i:i+3000] for i in range(0,len(text),3000)]
    return [enqueue(project,'telegram',{'text':f'{i+1}/{len(chunks)}\n'+part},[identity,i]) for i,part in enumerate(chunks)]


def enqueue_publication(project,bundle):
    cfg=project.config['notifications'];identity=bundle['operation_id']
    enqueue_text(project,'Опубликовано и проверено: '+bundle['title']+'\n'+bundle['url'],identity)
    for kind,enabled in [('indexnow',bool(cfg['indexnow_key'])),('yandex',cfg['yandex_recrawl']),('gsc',cfg['gsc_sitemap'])]:
        if enabled:enqueue(project,kind,{'url':bundle['url'],'operation_id':identity},identity)


def deliver(project,http,retry_unknown=False):
    if not http.live:raise Stop('Для отправки уведомлений требуется --live.')
    n=project.config['notifications'];s=project.config['sources'];results=[]
    rows=project.db.execute('SELECT * FROM deliveries WHERE status IN ("pending","unknown") ORDER BY rowid').fetchall()
    for row in rows:
        if row['status']=='unknown' and not retry_unknown:
            results.append({'id':row['id'],'status':'unknown','action':'Проверьте доставку; повтор только с --retry-unknown.'});continue
        p=json.loads(row['payload']);kind=row['kind']
        # Validate secret/config presence BEFORE marking an external attempt.
        if kind=='telegram':project.secret('TELEGRAM_BOT_TOKEN')
        elif kind=='yandex':project.secret('YANDEX_WEBMASTER_TOKEN')
        elif kind=='gsc':
            for key in ('GSC_CLIENT_ID','GSC_CLIENT_SECRET','GSC_REFRESH_TOKEN'):project.secret(key)
        with project.db:
            project.db.execute('UPDATE deliveries SET status="unknown",attempts=attempts+1 WHERE id=?',(row['id'],))
        try:
            if kind=='telegram':
                payload={'chat_id':n['telegram_chat_id'],'text':p['text'],'link_preview_options':{'is_disabled':True}}
                if n['telegram_thread_id']:payload['message_thread_id']=int(n['telegram_thread_id'])
                response=http.json('https://api.telegram.org/bot'+project.secret('TELEGRAM_BOT_TOKEN')+'/sendMessage',method='POST',data=payload)
                if not response.get('ok') or not response.get('result',{}).get('message_id'):raise Stop('Telegram не подтвердил доставку.')
            elif kind=='indexnow':
                from urllib.parse import urlsplit
                validate_url(n['indexnow_key_location'])
                if urlsplit(n['indexnow_key_location']).netloc!=urlsplit(project.config['site']['url']).netloc:
                    raise Stop('keyLocation должен находиться на сайте владельца.')
                http.request('https://api.indexnow.org/indexnow',method='POST',data={
                    'host':urlsplit(project.config['site']['url']).hostname,'key':n['indexnow_key'],
                    'keyLocation':n['indexnow_key_location'],'urlList':[p['url']]})
            elif kind=='yandex':
                if not s['yandex_user_id'] or not s['yandex_host_id']:raise Stop('Не настроен сайт Вебмастера.')
                http.json('https://api.webmaster.yandex.net/v4/user/'+quote(s['yandex_user_id'],safe='')+'/hosts/'+quote(s['yandex_host_id'],safe='')+'/recrawl/queue/',
                          method='POST',headers={'Authorization':'OAuth '+project.secret('YANDEX_WEBMASTER_TOKEN')},data={'url':p['url']})
            elif kind=='gsc':
                if not s['gsc_property']:raise Stop('Не настроено свойство GSC.')
                token=google_token(project,http)
                http.request('https://www.googleapis.com/webmasters/v3/sites/'+quote(s['gsc_property'],safe='')+'/sitemaps/'+quote(project.config['site']['url'].rstrip('/')+'/sitemap.xml',safe=''),
                             method='PUT',headers={'Authorization':'Bearer '+token})
            else:raise Stop('Неизвестный тип уведомления.')
            with project.db:project.db.execute('UPDATE deliveries SET status="sent",error="" WHERE id=?',(row['id'],))
            results.append({'id':row['id'],'status':'sent'});project.event('delivery_sent',row['id'])
        except (Stop,ValueError) as exc:
            message=str(exc) if isinstance(exc,Stop) else 'Неверные параметры уведомления.'
            with project.db:project.db.execute('UPDATE deliveries SET error=? WHERE id=?',(message,row['id']))
            results.append({'id':row['id'],'status':'unknown','reason':message})
    return results
