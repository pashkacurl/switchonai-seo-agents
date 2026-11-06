"""Article contracts and safe Markdown. Model output is data, never a program."""
from __future__ import annotations
import csv
import datetime as dt
from html import escape
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import sqlite3
from urllib.parse import urljoin, urlsplit
from .core import Stop, digest, now, safe_id, safe_slug, validate_url, write_json


class Page(HTMLParser):
    def __init__(self, url):
        super().__init__(convert_charrefs=True)
        self.url, self.title, self.h1, self.headings = url, '', [], []
        self.links, self.images, self.canonical, self.robots, self.description = [], [], '', '', ''
        self.text_parts, self.skip, self.tag, self.buffer, self.jsonld = [], 0, '', '', []
        self.scripts = ''

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ('script', 'style', 'noscript'):
            self.skip += 1
        if tag in ('title', 'h1', 'h2', 'h3'):
            self.tag, self.buffer = tag, ''
        if tag == 'a' and a.get('href'):
            self.links.append(urljoin(self.url, a['href']))
        if tag == 'img':
            self.images.append({'src': a.get('src', ''), 'alt': a.get('alt', '')})
        if tag == 'link' and 'canonical' in a.get('rel', '').lower().split():
            self.canonical = urljoin(self.url, a.get('href', ''))
        if tag == 'meta':
            if a.get('name', '').lower() in ('robots', 'googlebot'):
                self.robots += ' ' + a.get('content', '')
            if a.get('name', '').lower() == 'description':
                self.description = a.get('content', '')

    def handle_endtag(self, tag):
        if tag in ('script', 'style', 'noscript'):
            self.skip = max(0, self.skip - 1)
        if tag == self.tag:
            value = ' '.join(self.buffer.split())
            if tag == 'title':
                self.title = value
            elif tag == 'h1':
                self.h1.append(value)
            else:
                self.headings.append(value)
            self.tag, self.buffer = '', ''

    def handle_data(self, data):
        if not self.skip:
            self.text_parts.append(data)
            if self.tag:
                self.buffer += data

    @property
    def text(self):
        return ' '.join(' '.join(self.text_parts).split())


def csv_rows(path):
    with Path(path).open(encoding='utf-8-sig', newline='') as f:
        return list(csv.DictReader(f))


def import_topics(project, path):
    rows = csv_rows(path)
    if not rows:
        raise Stop('В CSV нет тем.')
    prepared = []
    for row in rows:
        for field in ('topic_id', 'cluster_id', 'page_id', 'slug', 'title', 'primary_query',
                      'query_ids', 'category', 'source_urls', 'serp_urls', 'evidence_status'):
            if field not in row or not row[field].strip():
                raise Stop(f'В строке темы не заполнено {field}. См. examples/topics.csv.')
        for field in ('topic_id', 'cluster_id', 'page_id'):
            safe_id(row[field])
        safe_slug(row['slug'])
        row['kind'] = row.get('kind') or 'new'
        if row['kind'] not in ('new', 'refresh'):
            raise Stop('kind: new или refresh.')
        if row['kind']=='refresh':
            if not re.fullmatch('[a-f0-9]{64}',row.get('previous_sha256','')):
                raise Stop('Обновлению нужен previous_sha256 текущего файла статьи.')
            try:dt.date.fromisoformat(row.get('original_date',''))
            except ValueError:raise Stop('Обновлению нужна исходная дата original_date YYYY-MM-DD.')
        for field in ('source_urls', 'serp_urls'):
            row[field] = [validate_url(x.strip()) for x in row[field].split('|') if x.strip()]
        row['query_ids'] = [safe_id(x.strip()) for x in row['query_ids'].split('|')]
        if row['evidence_status'] not in ('confirmed', 'hypothesis', 'waiver'):
            raise Stop('evidence_status: confirmed, hypothesis или waiver.')
        if row['evidence_status'] == 'waiver' and not row.get('owner_decision'):
            raise Stop('Для исключения требуется записанное owner_decision.')
        if row['evidence_status'] == 'confirmed' and not row.get('serp_run_id'):
            raise Stop('Для подтверждённой темы нужен serp_run_id из результата проверки выдачи.')
        prepared.append(row)
    try:
        with project.db:
            for row in prepared:
                old = project.db.execute('SELECT payload FROM topics WHERE id=?', (row['topic_id'],)).fetchone()
                if old:
                    if json.loads(old[0]) != row:
                        raise Stop('ID темы уже существует с другим содержимым; импорт не перезаписывает очередь.')
                    continue
                # A query cannot acquire two competing owners, including across imports.
                for previous in project.db.execute('SELECT payload FROM topics'):
                    p = json.loads(previous[0])
                    if row['kind']=='refresh' and (p['page_id']==row['page_id'] or p['slug']==row['slug']):
                        if any(p[k]!=row[k] for k in ('page_id','slug','cluster_id')):
                            raise Stop('Обновление должно сохранить владельца страницы, slug и кластер.')
                    if set(p['query_ids']) & set(row['query_ids']) and (row['kind']=='new' or p['page_id']!=row['page_id']):
                        raise Stop('Запрос уже принадлежит другой странице; уточните карту семантики.')
                status = 'queued' if row['evidence_status'] in ('confirmed', 'waiver') else 'needs_research'
                project.db.execute('INSERT INTO topics(id,cluster,page,slug,kind,status,payload,updated) VALUES(?,?,?,?,?,?,?,?)',
                    (row['topic_id'], row['cluster_id'], row['page_id'], row['slug'], row['kind'], status,
                     json.dumps(row, ensure_ascii=False), now()))
    except sqlite3.IntegrityError as exc:
        raise Stop('Дублируются slug, кластер или страница. У каждой новой страницы должен быть свой интент.') from exc
    project.event('topics_import', data={'rows': len(rows), 'sha256': digest(Path(path).read_bytes())})
    return len(rows)


def research(project, topic_id, http):
    row = project.topic(topic_id)
    topic = json.loads(row['payload'])
    if row['status'] not in ('queued', 'needs_research', 'blocked', 'researched'):
        raise Stop('Исследование не перезаписывает уже подготовленную или опубликованную статью.')
    if topic['evidence_status'] == 'hypothesis':
        raise Stop('Это гипотеза. Сначала проверьте выдачу и зафиксируйте решение через topic-verify.')
    approved = set(project.config['sources']['approved_urls'])
    missing = set(topic['source_urls']) - approved
    if missing:
        raise Stop('Источники ещё не согласованы: добавьте точные адреса через sources-approve.')
    result = {'topic': topic, 'at': now(), 'sources': [], 'competitors': [],
              'evidence_status': topic['evidence_status'], 'serp_run_id': topic.get('serp_run_id', '')}
    if row['kind']=='refresh':
        from .core import within
        if not topic.get('original_file'):raise Stop('Для обновления укажите original_file: копию текущей статьи внутри личного профиля.')
        original=within(project.root,topic['original_file']).read_bytes()
        if digest(original)!=topic['previous_sha256']:raise Stop('Копия исходной статьи не совпадает с previous_sha256.')
        result['original_article']=original.decode('utf-8')
    for index, url in enumerate(dict.fromkeys(topic['source_urls'] + topic['serp_urls'])):
        response = http.request(url)
        page = Page(response['url']); page.feed(response['text'])
        if len(page.text) < 80:
            raise Stop('Источник не содержит доступного текста: нужен другой источник или разрешённая выгрузка.')
        item = {'evidence_id': f'E{index+1}', 'url': url, 'final_url': response['url'],
                'retrieved_at': response['received_at'], 'sha256': digest(response['text'].encode()),
                'title': page.title, 'text': page.text[:18000], 'headings': page.headings,
                'observed_words': len(page.text.split()), 'observed_images': len(page.images)}
        if url in topic['source_urls']:
            # Cross-domain redirects require explicit source approval too.
            if response['url'] != url and response['url'] not in approved:
                raise Stop('Источник перенаправил на неутверждённый адрес; согласуйте конечный URL.')
            result['sources'].append(item)
        if url in topic['serp_urls']:
            result['competitors'].append(item)
    write_json(project.draft_dir(topic_id) / 'research.json', result)
    project.update_topic(topic_id, 'researched')
    return result


def check_article(article, research_data, project):
    fields = ('title', 'description', 'markdown', 'claims', 'media', 'changes')
    if not isinstance(article, dict) or any(k not in article for k in fields):
        raise Stop('Ответ автора не соответствует контракту статьи.')
    for key in ('title', 'description', 'markdown', 'changes'):
        if not isinstance(article[key], str):
            raise Stop('Текстовые поля статьи имеют неверный тип.')
    if not article['title'].strip() or not article['description'].strip() or len(article['markdown'].strip()) < 100:
        raise Stop('Статья не содержит необходимых заголовка, описания или текста.')
    text = article['markdown']
    if re.search(r'<|[{}]|^\s*(?:import|export)\s|^---\s*$', text, re.M):
        raise Stop('Модель вернула HTML/JSX/код или frontmatter; разрешён безопасный Markdown.')
    if re.search(r'\]\((?:javascript:|data:|file:)', text, re.I):
        raise Stop('Небезопасная ссылка в статье.')
    evidence = {x['evidence_id']: x for x in research_data['sources']}
    if not isinstance(article['claims'], list) or not article['claims']:
        raise Stop('Автор не представил реестр проверяемых утверждений.')
    for claim in article['claims']:
        if not isinstance(claim, dict) or not all(isinstance(claim.get(k), str) for k in ('claim', 'evidence_id', 'quote')):
            raise Stop('Неполная запись утверждения.')
        source = evidence.get(claim['evidence_id'])
        if not source or not claim['quote'].strip() or claim['quote'].casefold() not in source['text'].casefold():
            raise Stop('Цитата-доказательство не найдена в полученном источнике.')
        if claim['claim'] not in text:
            raise Stop('Реестр фактов не соответствует тексту статьи.')
    allowed = {x['url'] for x in research_data['sources']} | {x['final_url'] for x in research_data['sources']}
    own = project.config['site']['url'].rstrip('/')
    if re.search(r'^\s*\[[^\]]+\]:|\]\s*\[',text,re.M):
        raise Stop('Используйте только обычные inline Markdown-ссылки; reference-ссылки не поддерживаются.')
    for url in re.findall(r'(?<!!)\[[^\]]+\]\(([^)\s]+)\)', text):
        if url.startswith('/') and not url.startswith('//'):
            continue
        if url not in allowed and not url.startswith(own + '/'):
            raise Stop('В статье ссылка, которой нет среди утверждённых источников или страниц сайта.')
    if re.search(r'!\[[^\]]*\]\(', text):
        raise Stop('Медиа передаются отдельным полем, а не произвольными изображениями в Markdown.')
    if not isinstance(article['media'], list):
        raise Stop('media должен быть массивом схем.')
    for media in article['media']:
        if not isinstance(media, dict) or media.get('type') != 'steps' or not isinstance(media.get('items'), list):
            raise Stop('Поддерживается схема steps с текстовыми этапами.')
        if not 2 <= len(media['items']) <= 6 or not all(isinstance(x, str) and 0 < len(x) <= 75 for x in media['items']):
            raise Stop('Для схемы нужно 2–6 коротких содержательных этапов.')
        if not isinstance(media.get('alt'), str) or not media['alt'].strip() or re.search(r'[\[\]{}<>\r\n\\]',media['alt']):
            raise Stop('Схеме нужен содержательный alt.')
    return article


def diagram(media):
    items = media['items']; height = 100 + 100 * len(items)
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="800" height="{height}" viewBox="0 0 800 {height}" role="img">',
             f'<title>{escape(media["alt"])}</title><rect width="800" height="{height}" fill="#f4f7fb"/>']
    for i, value in enumerate(items):
        y = 40 + i * 100
        parts.append(f'<rect x="24" y="{y}" width="752" height="76" rx="12" fill="white"/>')
        parts.append(f'<text x="44" y="{y+44}" font-family="sans-serif" font-size="20" fill="#006cff">{i+1:02}</text>')
        # Fixed line wrapping prevents arbitrary SVG content and clipped long labels.
        words, lines, line = value.split(), [], ''
        for word in words:
            if len(line + ' ' + word) > 43:
                lines.append(line); line = word
            else:
                line = (line + ' ' + word).strip()
        lines.append(line)
        for j, line in enumerate(lines[:2]):
            parts.append(f'<text x="95" y="{y+30+j*24}" font-family="sans-serif" font-size="20" fill="#0b1320">{escape(line)}</text>')
    return ''.join(parts) + '</svg>'


def materialize(project, topic_id, article):
    row = project.topic(topic_id); topic = json.loads(row['payload'])
    config = project.config['site']; date = project.day()
    directory = project.draft_dir(topic_id); directory.mkdir(parents=True, exist_ok=True)
    revision = digest({'article':article,'topic_id':topic_id,'page_id':row['page']})
    fields = {'slug': row['slug'], 'title': article['title'], 'description': article['description'],
              'date': topic.get('original_date') or date, 'lastmod': date, 'author': config['author'],
              'category': topic['category'], 'draft': False, 'cluster_id': row['cluster'], 'page_id': row['page'],
              'seo_revision':revision}
    if not fields['author']:
        raise Stop('В профиле не указан реальный автор/редакция.')
    if row['kind'] == 'refresh' and (not topic.get('original_date') or not article['changes'].strip()):
        raise Stop('Обновлению нужны первоначальная дата и описание содержательного изменения.')
    md = '---\n' + '\n'.join(k + ': ' + json.dumps(v, ensure_ascii=False) for k, v in fields.items()) + '\n---\n\n'
    md += article['markdown'].strip() + '\n'
    media_files = []
    for i, media in enumerate(article['media']):
        filename = f'{row["slug"]}-{i+1}.svg'
        svg = diagram(media)
        (directory / filename).write_bytes(svg.encode('utf-8'))
        md += f'\n![{media["alt"].replace("]", "")}]( /media/seo/{filename})\n'.replace(']( ', '](')
        media_files.append(filename)
    path = directory / 'article.mdx'; path.write_bytes(md.encode('utf-8'))
    bundle = {'topic_id': topic_id, 'cluster_id': row['cluster'], 'page_id': row['page'],
              'slug': row['slug'], 'kind': row['kind'], 'title': article['title'],
              'url': config['url'].rstrip('/') + config['blog_path'] + row['slug'] + '/',
              'content_sha256': digest(md.encode()), 'revision':revision,'media': media_files, 'created_at': now(),
              'previous_sha256':topic.get('previous_sha256','')}
    bundle['operation_id'] = digest([config['url'], topic_id, bundle['content_sha256']])[:40]
    bundle['media_sha256'] = {name: digest((directory/name).read_bytes()) for name in media_files}
    write_json(directory / 'bundle.json', bundle)
    return bundle
