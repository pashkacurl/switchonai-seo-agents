"""Data providers keep observations separate; they never manufacture demand."""
from __future__ import annotations
import datetime as dt
import json
from pathlib import Path
from urllib.parse import quote, urlencode
from .core import Stop, now, read_json, validate_url


def google_token(project, http):
    data = http.json('https://oauth2.googleapis.com/token', method='POST', form={
        'client_id': project.secret('GSC_CLIENT_ID'), 'client_secret': project.secret('GSC_CLIENT_SECRET'),
        'refresh_token': project.secret('GSC_REFRESH_TOKEN'), 'grant_type': 'refresh_token'})
    if not data.get('access_token'):
        raise Stop('Google не вернул access token; проверьте разрешения аккаунта.')
    return data['access_token']


def gsc(project, http):
    prop = project.config['sources']['gsc_property']
    if not prop:
        raise Stop('GSC property не настроен.')
    token = google_token(project, http)
    end = dt.date.fromisoformat(project.day()) - dt.timedelta(days=3)
    start = end - dt.timedelta(days=6)
    rows = []; offset = 0
    # Explicit bounded sample, not a claim that GSC exposes a complete semantic core.
    while offset < 5000:
        data = http.json('https://www.googleapis.com/webmasters/v3/sites/' + quote(prop, safe='') + '/searchAnalytics/query',
            method='POST', headers={'Authorization': 'Bearer ' + token},
            data={'startDate': str(start), 'endDate': str(end), 'dimensions': ['query','page','country','device'],
                  'type': 'web', 'rowLimit': 1000, 'startRow': offset, 'dataState': 'final'})
        batch = data.get('rows', [])
        rows.extend(batch)
        if len(batch) < 1000:
            break
        offset += 1000
    return {'source':'gsc', 'property':prop, 'start':str(start), 'end':str(end), 'rows':rows,
            'measurement':'average_position', 'scope':'top rows returned by GSC, max 5000', 'at':now()}


def yandex(project, http):
    s = project.config['sources']; uid, hid = s['yandex_user_id'], s['yandex_host_id']
    if not uid or not hid:
        raise Stop('Не настроены идентификаторы пользователя/сайта Вебмастера.')
    url = 'https://api.webmaster.yandex.net/v4/user/' + quote(uid, safe='') + '/hosts/' + quote(hid, safe='')
    data = http.json(url + '/search-queries/popular/?order_by=TOTAL_SHOWS&query_indicator=TOTAL_SHOWS&query_indicator=TOTAL_CLICKS&limit=500',
                     headers={'Authorization': 'OAuth ' + project.secret('YANDEX_WEBMASTER_TOKEN')})
    return {'source':'yandex_webmaster', 'at':now(), 'host':hid, 'response':data,
            'measurement':'site_queries', 'scope':'popular queries returned by provider; not Wordstat demand'}


def topvisor(project, http):
    s = project.config['sources']
    if not s['topvisor_user_id'] or not s['topvisor_project_id'] or s['topvisor_region_index'] is None:
        raise Stop('Нужны отдельные user_id, project_id и region_index Topvisor.')
    data = http.json('https://api.topvisor.com/v2/json/get/positions_2/history', method='POST',
        headers={'User-Id':str(s['topvisor_user_id']), 'Authorization':'bearer '+project.secret('TOPVISOR_API_TOKEN')},
        data={'project_id':int(s['topvisor_project_id']), 'regions_indexes':[int(s['topvisor_region_index'])],
              'dates':[project.day(),project.day()], 'show_headers':1})
    if data.get('errors') or not isinstance(data.get('result'), dict):
        raise Stop('Topvisor не вернул успешную историю; запуск нового платного съёма не выполнялся.')
    return {'source':'topvisor', 'at':now(), 'date':project.day(), 'project':str(s['topvisor_project_id']),
            'region_index':s['topvisor_region_index'], 'response':data['result'],
            'scope':'existing history only; engine/region/device must be read from returned headers'}


def metrika(project, http):
    counter = project.config['sources']['metrika_counter_id']
    if not counter:
        raise Stop('Счётчик Метрики не настроен.')
    end = dt.date.fromisoformat(project.day()) - dt.timedelta(days=1)
    start = end - dt.timedelta(days=6)
    url = 'https://api-metrika.yandex.net/stat/v1/data?' + urlencode({
        'ids':counter, 'metrics':'ym:s:visits,ym:s:users,ym:s:anyGoalConversionRate',
        'date1':str(start), 'date2':str(end), 'accuracy':'full'})
    data = http.json(url, headers={'Authorization':'OAuth '+project.secret('METRIKA_TOKEN')})
    if data.get('errors'):
        raise Stop('Метрика вернула ошибку; данные не приняты.')
    return {'source':'metrika','at':now(),'start':str(start),'end':str(end), 'counter':counter, 'response':data}


def psi(project, http):
    key = project.secret('PSI_API_KEY')
    # Key stays in an auth header; errors never include the request URL or headers.
    url = 'https://www.googleapis.com/pagespeedonline/v5/runPagespeed?' + urlencode({
        'url':project.config['site']['url'], 'strategy':'mobile', 'category':'performance'})
    data = http.json(url, headers={'X-Goog-Api-Key':key})
    if data.get('error'):
        raise Stop('PageSpeed вернул ошибку; измерение не принято.')
    return {'source':'pagespeed', 'at':now(), 'url':project.config['site']['url'],
            'field':data.get('loadingExperience'), 'lab':data.get('lighthouseResult'),
            'note':'Полевые данные и лабораторные результаты — разные измерения.'}


def geo_import(project):
    filename = project.config['sources']['geo_import']
    if not filename:
        raise Stop('Импорт наблюдений GEO не настроен.')
    p = Path(filename)
    if not p.is_absolute():
        p = project.root / p
    data = read_json(p)
    if not isinstance(data, list):
        raise Stop('GEO-импорт должен содержать массив наблюдений.')
    for item in data:
        if not isinstance(item,dict) or not all(item.get(k) for k in ('question','provider','observed_at','answer','source_run_id')):
            raise Stop('GEO-наблюдению нужны вопрос, провайдер, дата, ответ и source_run_id.')
        try:
            dt.datetime.fromisoformat(item['observed_at'])
        except (ValueError,TypeError) as exc:
            raise Stop('Неверная дата GEO-наблюдения.') from exc
    return {'source':'provided_geo_export', 'at':now(), 'observations':data,
            'note':'Импорт пользователя; ответы не снимались заново.'}
