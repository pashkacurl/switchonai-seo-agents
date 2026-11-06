"""Read-only observation modules and evidence-preserving report generation."""
from __future__ import annotations
import csv
import datetime as dt
import json
import math
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser
import xml.etree.ElementTree as ET
from .content import Page, csv_rows
from .core import Stop, digest, now, read_json, write_json
from . import providers


def sitemap_urls(http, url, limit, seen=None):
    seen = seen or set()
    if url in seen or len(seen) >= 4:
        return []
    seen.add(url)
    response = http.request(url)
    try:
        root = ET.fromstring(response['text'])
    except ET.ParseError as exc:
        raise Stop('Sitemap не является XML.') from exc
    if root.tag.endswith('sitemapindex'):
        result = []
        for child in root:
            loc = next((x.text for x in child if x.tag.endswith('loc')), '')
            if loc and urlsplit(loc).netloc == urlsplit(url).netloc:
                result += sitemap_urls(http, loc, limit - len(result), seen)
            if len(result) >= limit:
                break
        return result[:limit]
    return [x.text for x in root.iter() if x.tag.endswith('loc') and x.text][:limit]


def crawl(project, http):
    site = project.config['site']['url'].rstrip('/')
    limit = min(500, project.config['limits']['crawl_pages'])
    if not limit:
        raise Stop('Задайте limits.crawl_pages.')
    issues, pages, robots_text = [], [], None
    try:
        robots_text = http.request(site + '/robots.txt')['text']
    except Stop as exc:
        issues.append({'url':site+'/robots.txt','status':'Не проверено','detail':str(exc)})
    urls = [site + '/']
    try:
        urls += sitemap_urls(http, site+'/sitemap.xml', limit)
    except Stop as exc:
        issues.append({'url':site+'/sitemap.xml','status':'Не проверено','detail':str(exc)})
    urls = list(dict.fromkeys(urls))[:limit]
    robots = RobotFileParser(); robots.parse((robots_text or '').splitlines())
    for url in urls:
        if urlsplit(url).netloc != urlsplit(site).netloc:
            continue
        if robots_text is not None and not robots.can_fetch('SwitchOnAI-SEO-Agents', url):
            issues.append({'url':url,'status':'Предупреждение','detail':'robots.txt запрещает обход; содержимое не скачивалось.'})
            continue
        try:
            response = http.request(url); page = Page(response['url']); page.feed(response['text'])
            notes = []
            if not page.title: notes.append('Отсутствует title')
            if not page.description: notes.append('Отсутствует description')
            if len(page.h1) != 1: notes.append(f'H1: {len(page.h1)}; проверьте структуру страницы')
            if not page.canonical: notes.append('Отсутствует canonical')
            if 'noindex' in page.robots.lower() or 'noindex' in response.get('headers',{}).get('X-Robots-Tag','').lower():
                notes.append('Обнаружен noindex; проверьте назначение страницы')
            if any(not x['alt'] for x in page.images): notes.append('Есть изображения без alt; проверьте смысловые изображения')
            if not page.text: notes.append('В HTML не найден доступный текст')
            pages.append({'url':url,'final_url':response['url'],'status':response['status'],'title':page.title,
                'description':page.description,'canonical':page.canonical,'h1':page.h1,'links':page.links,
                'text_sha256':digest(page.text.encode()),'observed_author':bool(project.config['site']['author'] and
                    project.config['site']['author'].casefold() in page.text.casefold()),
                'observed_date':bool(__import__('re').search(r'20\d{2}[-./]\d{2}[-./]\d{2}',page.text)),
                'notes':notes,'at':response['received_at']})
            issues.extend({'url':url,'status':'Предупреждение','detail':n} for n in notes)
        except Stop as exc:
            issues.append({'url':url,'status':'Не проверено','detail':str(exc)})
    if not pages:
        raise Stop('Не получено ни одной страницы сайта; аудит не завершён.')
    known = {x['url'].rstrip('/') for x in pages}
    incoming = {url:0 for url in known}
    for page in pages:
        for link in set(page['links']):
            clean = link.split('#')[0].split('?')[0].rstrip('/')
            if clean in incoming and clean != page['url'].rstrip('/'):
                incoming[clean] += 1
    orphan_candidates = [u for u,n in incoming.items() if not n and u != site]
    return {'at':now(),'pages':pages,'issues':issues,'orphan_candidates':orphan_candidates,
            'scope':f'Не более {limit} URL; вывод о сиротах относится только к обследованному графу.',
            'trust_note':'Наличие имени и даты — наблюдения, не оценка квалификации автора или E-E-A-T.'}


def competitors(project, http):
    observations = []
    for url in project.config['sources']['competitor_urls']:
        response = http.request(url); page = Page(response['url']); page.feed(response['text'])
        observations.append({'url':url,'at':now(),'title':page.title,'headings':page.headings,
                             'text_sha256':digest(page.text.encode()),'links':page.links})
    if not observations:
        raise Stop('URL конкурентов не настроены.')
    previous = project.state_dir/'competitors.json'
    old = read_json(previous) if previous.exists() else {'observations':[]}
    old_by_url = {x['url']:x for x in old['observations']}
    result = {'observations':observations,'changed':[x['url'] for x in observations if x['url'] in old_by_url
                  and x['text_sha256'] != old_by_url[x['url']]['text_sha256']],
              'baseline':not bool(old_by_url)}
    write_json(previous,result)
    return result


def measurements(project):
    filename = project.config['sources']['measurements_import']
    if not filename:
        raise Stop('Импорт измерений позиций не настроен.')
    path = Path(filename); path = path if path.is_absolute() else project.root/path
    rows = csv_rows(path)
    needed = ('query_id','source_run_id','source','engine','region','device','depth','metric',
              'date_start','date_end','position','result_status','target_url')
    groups = {}; ids = set()
    for row in rows:
        if any(k not in row for k in needed):
            raise Stop('Неполная схема measurements.csv; сравнение остановлено.')
        if row['metric'] not in ('serp_position','average_position') or row['result_status'] not in ('observed','not_in_top','unknown'):
            raise Stop('Неверный тип измерения или статус.')
        ident = (row['source_run_id'],row['query_id'])
        if ident in ids:
            raise Stop('Повтор измерения query_id/source_run_id.')
        ids.add(ident)
        start,end = dt.date.fromisoformat(row['date_start']),dt.date.fromisoformat(row['date_end'])
        if start>end or end>dt.date.fromisoformat(project.day()):
            raise Stop('Некорректный период измерения.')
        if row['result_status']=='observed':
            try: position=float(row['position'])
            except ValueError as exc: raise Stop('Некорректная позиция.') from exc
            if not math.isfinite(position) or position<=0: raise Stop('Позиция должна быть положительным числом.')
        key=tuple(row[k] for k in ('query_id','source','engine','region','device','depth','metric','target_url'))+((end-start).days,)
        groups.setdefault(key,[]).append(row)
    changes=[]
    for key, group in groups.items():
        group.sort(key=lambda x:(x['date_end'],x['date_start']))
        if len(group)<2:continue
        before,after=group[-2:]
        if after['date_end']==before['date_end']:continue
        item={'query_id':after['query_id'],'from':before['date_end'],'to':after['date_end'],
              'metric':after['metric'],'source':after['source'],'status':after['result_status']}
        if before['result_status']==after['result_status']=='observed':
            item['delta']=float(after['position'])-float(before['position'])
        elif before['result_status']=='observed' and after['result_status']=='not_in_top':
            item['detail']='Не найден в заданной глубине; точная величина падения неизвестна.'
        else: item['detail']='Недостаточно сопоставимых наблюдений; стабильность не установлена.'
        changes.append(item)
    return {'source':'provided_measurements','at':now(),'rows':rows,'changes':changes,
            'note':'Частотности не суммируются; исторические измерения сохраняются отдельно.'}


def collect(project, http, weekly=False, model=None):
    modules={'technical':lambda:crawl(project,http),'gsc':lambda:providers.gsc(project,http),
             'yandex':lambda:providers.yandex(project,http),'topvisor':lambda:providers.topvisor(project,http),
             'metrika':lambda:providers.metrika(project,http),'measurements':lambda:measurements(project)}
    if weekly:
        modules.update(competitors=lambda:competitors(project,http),psi=lambda:providers.psi(project,http),
                       geo=lambda:providers.geo_import(project))
    report={'at':now(),'site':project.config['site']['url'],'modules':{},'candidates':[]}
    for name,fn in modules.items():
        try:
            value=fn();report['modules'][name]={'status':'OK','data':value}
        except (Stop, ValueError, KeyError,TypeError) as exc:
            report['modules'][name]={'status':'Не проверено','reason':str(exc) if isinstance(exc,Stop) else 'Ответ не соответствует контракту источника.'}
    g=report['modules'].get('gsc',{}).get('data',{})
    if any(not isinstance(row,dict) or not isinstance(row.get('keys'),list) or len(row['keys'])!=4 or not all(isinstance(k,str) for k in row['keys']) for row in g.get('rows',[])):
        report['modules']['gsc']={'status':'Не проверено','reason':'GSC вернул неполные измерения query/page/country/device.'}
        g={}
    owned=[]
    for row in project.db.execute('SELECT payload FROM topics'):
        owned.append(json.loads(row[0]).get('primary_query','').casefold())
    for row in g.get('rows',[]):
        if row.get('keys') and row['keys'][0].casefold() not in owned:
            report['candidates'].append({'query':row['keys'][0],'source':'gsc','start':g['start'],'end':g['end'],
                'observed_page':row['keys'][1],'country':row['keys'][2],'device':row['keys'][3],
                'impressions':row.get('impressions'),'clicks':row.get('clicks'),'status':'needs_SERP_and_ownership_review'})
    y=report['modules'].get('yandex',{}).get('data',{}).get('response',{})
    for row in y.get('queries',[]):
        if row.get('query_text') and row['query_text'].casefold() not in owned:
            report['candidates'].append({'query':row['query_text'],'source':'yandex_webmaster',
                'start':y.get('date_from'),'end':y.get('date_to'),'device':'ALL',
                'impressions':row.get('indicators',{}).get('TOTAL_SHOWS'),
                'clicks':row.get('indicators',{}).get('TOTAL_CLICKS'),'status':'needs_SERP_and_ownership_review'})
    report['priorities']=[]
    technical=report['modules']['technical'].get('data',{})
    for issue in technical.get('issues',[])[:10]:
        report['priorities'].append({'action':issue['detail'],'basis':issue['url'],'source':'technical'})
    for change in report['modules']['measurements'].get('data',{}).get('changes',[]):
        if change.get('delta',0)>0 or change.get('status')=='not_in_top':
            report['priorities'].append({'action':'Проверить изменение позиции: '+json.dumps(change,ensure_ascii=False),
                                        'basis':change['query_id'],'source':'measurements'})
    if report['candidates']:
        report['priorities'].append({'action':'Проверить кандидатов по выдаче и карте страниц до добавления в очередь.',
                                    'basis':'candidates','source':'gsc'})
    if model:
        try:
            # Do not send full HTML, secrets or query-bearing auth URLs to the strategist.
            report['strategy']=model.generate('strategist',{'priorities':report['priorities'],
                'availability':{k:v['status'] for k,v in report['modules'].items()},
                'candidate_count':len(report['candidates'])})
        except Stop as exc: report['strategy']={'status':'Не проверено','reason':str(exc)}
    report_id=dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    directory=project.root/'reports';write_json(directory/(report_id+'.json'),report)
    lines=['# SEO-аудит '+report['site'],'','Дата: '+report['at'],'',
           '## Критичные проблемы (P1)','','Подтверждённых блокирующих проблем по собранным наблюдениям не установлено. Это не заключение об отсутствии проблем.','',
           '## Важные замечания (P2)','','Приоритет P2 автоматически не назначается: влияние наблюдений требует проверки в контексте сайта.','',
           '## Рекомендации (P3)','']
    lines += [f'- {p["action"]} (основание: {p["basis"]}; статус вывода: косвенно подтверждено)' for p in report['priorities']]
    if not report['priorities']:lines.append('Приоритеты не сформированы; ознакомьтесь с непроверенными модулями ниже.')
    lines+=['','## Детальные результаты','','| Элемент | Значение | Статус | Рекомендация |','|---|---|---|---|']
    for name,data in report['modules'].items():
        detail=data.get('reason','Наблюдения и параметры сохранены в '+report_id+'.json, modules.'+name)
        recommendation='Проверить доступ и повторить модуль в согласованном лимите.' if data['status']!='OK' else 'Проверить наблюдения и применимость к назначению страниц.'
        lines.append(f'| {name} | {detail.replace("|", "/")} | {data["status"]} | {recommendation} |')
    if report.get('strategy',{}).get('summary'):lines+=['','## Предложения стратега','',report['strategy']['summary']]
    lines+=['','Источники и границы каждого измерения сохранены в JSON. Отправка отчёта — отдельное действие.']
    (directory/(report_id+'.md')).write_text('\n'.join(lines)+'\n',encoding='utf-8')
    write_json(project.state_dir/'last-report.json',{'id':report_id,'path':str(directory/(report_id+'.md')),
                                                  'at':report['at'],'technical_ok':report['modules']['technical']['status']=='OK'})
    return report_id,report
