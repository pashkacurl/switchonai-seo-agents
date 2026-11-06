"""Configuration, private state, atomic writes and cross-process exclusion."""
from __future__ import annotations
import contextlib
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class Stop(RuntimeError):
    """An actionable error safe to show without provider response bodies."""


def process_exists(pid):
    if type(pid) is not int or pid<=0:raise Stop('Некорректный PID.')
    if os.name=='nt':
        import ctypes
        from ctypes import wintypes
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
        kernel.OpenProcess.restype=wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes=[wintypes.HANDLE,ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes=[wintypes.HANDLE]
        handle=kernel.OpenProcess(0x1000,False,pid)
        if not handle:
            if ctypes.get_last_error()==87:return False
            raise Stop('Не удалось проверить PID; блокировка сохранена.')
        try:
            code=wintypes.DWORD()
            if not kernel.GetExitCodeProcess(handle,ctypes.byref(code)):raise Stop('Не удалось проверить состояние процесса.')
            return code.value==259
        finally:kernel.CloseHandle(handle)
    try:os.kill(pid,0)
    except ProcessLookupError:return False
    except OSError as exc:raise Stop('Не удалось подтвердить отсутствие процесса.') from exc
    return True


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def digest(value):
    data = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    return hashlib.sha256(data).hexdigest()


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8-sig'))
    except (OSError, ValueError) as exc:
        raise Stop(f'Не удалось прочитать JSON: {Path(path).name}') from exc


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp-' + str(os.getpid()))
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def safe_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,99}', value):
        raise Stop('ID: допустимы латиница, цифры, дефис и подчёркивание; до 100 знаков.')
    return value


def safe_slug(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', value) or len(value) > 120:
        raise Stop('Slug должен состоять из строчных латинских букв, цифр и дефисов.')
    return value


def within(root, relative):
    root = Path(root).resolve()
    path = (root / relative).resolve()
    if not path.is_relative_to(root) or path == root:
        raise Stop('Путь выходит за пределы разрешённой папки.')
    return path


def validate_url(url):
    p = urlsplit(url)
    if p.scheme != 'https' or not p.hostname or p.username or p.password or p.port not in (None, 443):
        raise Stop('Требуется HTTPS URL без логина, пароля и нестандартного порта.')
    if p.fragment:
        raise Stop('Укажите URL источника без фрагмента #.')
    return url


def timezone(name):
    # Fixed offsets are portable when system tzdata is absent on Windows.
    if name == 'UTC':
        return dt.timezone.utc
    m = re.fullmatch(r'UTC([+-])(\d{2}):(\d{2})', name)
    if m:
        hours, minutes = int(m[2]), int(m[3])
        if hours > 14 or minutes > 59:
            raise Stop('Некорректный часовой пояс.')
        return dt.timezone((1 if m[1] == '+' else -1) * dt.timedelta(hours=hours, minutes=minutes))
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise Stop('Нет базы часовых поясов. Используйте UTC или UTC+03:00 либо установите системную tzdata.') from exc


def default_config():
    return {
        'schema_version': 1, 'site': {'url': 'https://example.com', 'language': 'ru',
            'market': '', 'region': '', 'device': 'desktop', 'blog_path': '/blog/',
            'author': '', 'business_file': 'business.md'},
        'timezone': 'UTC+03:00',
        'limits': {'requests_per_day': 0, 'model_calls_per_day': 0, 'output_tokens_per_day': 0,
                   'max_input_chars': 90000, 'max_output_tokens': 6000, 'articles_per_day': 0, 'updates_per_day': 0,
                   'crawl_pages': 30, 'source_age_days': 30, 'http_timeout': 30},
        'model': {'provider': 'anthropic', 'name': '', 'codex_executable': 'codex'},
        'sources': {'approved_urls': [], 'competitor_urls': [], 'search_engine': '',
                    'gsc_property': '', 'yandex_user_id': '', 'yandex_host_id': '',
                    'topvisor_user_id': '', 'topvisor_project_id': '', 'topvisor_region_index': None,
                    'metrika_counter_id': '', 'geo_import': '', 'measurements_import': ''},
        'publish': {'mode': 'review', 'adapter': 'local', 'site_config': '', 'ssh_command': [],
                    'verified_trial': '', 'max_review_attempts': 2},
        'notifications': {'telegram_chat_id': '', 'telegram_thread_id': '', 'indexnow_key': '',
                          'indexnow_key_location': '', 'yandex_recrawl': False, 'gsc_sitemap': False},
        'schedule': {'enabled': False, 'reports_enabled': False, 'content_enabled': False,
                     'report_hour': 8, 'content_start_hour': 9, 'weekly_day': 0,'strategy_enabled':False},
    }


ENV_NAMES = ('ANTHROPIC_API_KEY', 'GSC_CLIENT_ID', 'GSC_CLIENT_SECRET', 'GSC_REFRESH_TOKEN',
             'YANDEX_WEBMASTER_TOKEN', 'PSI_API_KEY', 'TOPVISOR_API_TOKEN', 'METRIKA_TOKEN',
             'TELEGRAM_BOT_TOKEN')


def initialize(root):
    root = Path(root).resolve()
    package=Path(__file__).resolve().parents[1]
    if root==package or root.is_relative_to(package):
        raise Stop('Личный профиль должен находиться вне распространяемого репозитория.')
    root.mkdir(parents=True, exist_ok=True)
    if os.name!='nt':os.chmod(root,0o700)
    config = root / 'profile.json'
    if config.exists():
        raise Stop('Профиль уже существует; init ничего не перезаписывает.')
    ignore = root / '.gitignore'
    existing = ignore.read_text(encoding='utf-8') if ignore.exists() else ''
    for line in ('.env', 'state/', 'reports/', 'drafts/', 'inputs/', 'profile.json', 'business.md'):
        if line not in existing.splitlines():
            existing += '\n' + line
    ignore.write_text(existing.lstrip('\n') + '\n', encoding='utf-8')
    write_json(config, default_config())
    example = '\n'.join(name + '=' for name in ENV_NAMES) + '\n'
    (root / '.env.example').write_text(example, encoding='utf-8')
    if not (root / '.env').exists():
        (root / '.env').write_text(example, encoding='utf-8')
        os.chmod(root / '.env', 0o600)
    (root / 'business.md').write_text('# Фактура бизнеса\n\nУкажите реальные продукт, аудиторию, предложение, автора и ограничения.\n', encoding='utf-8')
    return config


class Project:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.config = read_json(self.root / 'profile.json')
        if self.config.get('schema_version') != 1:
            raise Stop('Неподдерживаемая версия профиля; проверьте инструкцию обновления.')
        defaults = default_config()
        for section, values in defaults.items():
            if isinstance(values, dict):
                if not isinstance(self.config.get(section), dict):
                    raise Stop(f'В профиле отсутствует раздел {section}.')
                for key in values:
                    if key not in self.config[section]:
                        raise Stop(f'В профиле отсутствует {section}.{key}.')
        self.tz = timezone(self.config['timezone'])
        validate_url(self.config['site']['url'])
        for name, value in self.config['limits'].items():
            if type(value) is not int or value < 0:
                raise Stop(f'Лимит {name}: требуется целое неотрицательное число.')
        if not 1 <= self.config['limits']['http_timeout'] <= 120:
            raise Stop('http_timeout: от 1 до 120 секунд.')
        if self.config['publish']['mode'] not in ('review', 'automatic'):
            raise Stop('publish.mode: review или automatic.')
        for key in ('report_hour', 'content_start_hour'):
            if type(self.config['schedule'][key]) is not int or not 0 <= self.config['schedule'][key] <= 23:
                raise Stop('Час расписания: 0–23.')
        for key in ('enabled','reports_enabled','content_enabled','strategy_enabled'):
            if type(self.config['schedule'][key]) is not bool:raise Stop('schedule.'+key+' должен быть boolean, не строкой.')
        site=self.config['site']
        if not isinstance(site['blog_path'],str) or not site['blog_path'].startswith('/') or not site['blog_path'].endswith('/') or any(x in site['blog_path'] for x in ('..','?','#','\\')):
            raise Stop('Неверный путь site.blog_path.')
        if type(self.config['publish']['max_review_attempts']) is not int or not 1<=self.config['publish']['max_review_attempts']<=5:
            raise Stop('max_review_attempts: целое от 1 до 5.')
        if self.config['schedule']['weekly_day'] not in range(7):
            raise Stop('weekly_day: 0 (понедельник) — 6 (воскресенье).')
        self.state_dir = self.root / 'state'
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.state_dir / 'state.sqlite3', timeout=20)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS topics(id TEXT PRIMARY KEY, cluster TEXT NOT NULL, page TEXT NOT NULL,
          slug TEXT NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL, payload TEXT NOT NULL,
          updated TEXT NOT NULL, error TEXT NOT NULL DEFAULT '');
        CREATE UNIQUE INDEX IF NOT EXISTS one_cluster ON topics(cluster) WHERE kind='new';
        CREATE UNIQUE INDEX IF NOT EXISTS one_page ON topics(page) WHERE kind='new';
        CREATE UNIQUE INDEX IF NOT EXISTS one_slug ON topics(slug) WHERE kind='new';
        CREATE UNIQUE INDEX IF NOT EXISTS active_refresh ON topics(page) WHERE kind='refresh' AND status NOT IN ('published','rolled_back');
        CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, at TEXT, kind TEXT, object_id TEXT, data TEXT);
        CREATE TABLE IF NOT EXISTS budgets(day TEXT, kind TEXT, used INTEGER, PRIMARY KEY(day,kind));
        CREATE TABLE IF NOT EXISTS deliveries(id TEXT PRIMARY KEY, kind TEXT, payload TEXT, status TEXT,
          attempts INTEGER DEFAULT 0, error TEXT DEFAULT '');
        CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, at TEXT, status TEXT, payload TEXT);
        CREATE TABLE IF NOT EXISTS locks(name TEXT PRIMARY KEY, pid INTEGER, token TEXT);
        ''')
        self.db.commit()
        self.secrets = {}
        env = self.root / '.env'
        if env.exists():
            for line in env.read_text(encoding='utf-8-sig').splitlines():
                if not line.strip() or line.lstrip().startswith('#') or '=' not in line:
                    continue
                name, value = line.split('=', 1)
                if name.strip() in ENV_NAMES:
                    self.secrets[name.strip()] = value.strip().strip('"\'')
        for name in ENV_NAMES:
            if os.environ.get(name):
                self.secrets[name] = os.environ[name]

    def close(self):
        self.db.close()

    def day(self):
        return dt.datetime.now(self.tz).date().isoformat()

    def secret(self, name):
        value = self.secrets.get(name, '')
        if not value:
            raise Stop(f'Не заполнена переменная {name}; значение вводится в .env.')
        return value

    def event(self, kind, object_id='', data=None):
        self.db.execute('INSERT INTO events(at,kind,object_id,data) VALUES(?,?,?,?)',
                        (now(), kind, object_id, json.dumps(data or {}, ensure_ascii=False)))
        self.db.commit()

    def reserve(self, kind, amount=1):
        limit = self.config['limits'][kind + '_per_day']
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            row = self.db.execute('SELECT used FROM budgets WHERE day=? AND kind=?', (self.day(), kind)).fetchone()
            used = row[0] if row else 0
            if not limit or used + amount > limit:
                raise Stop(f'Дневной лимит {kind} не задан или исчерпан; запрос не отправлен.')
            self.db.execute('INSERT INTO budgets VALUES(?,?,?) ON CONFLICT(day,kind) DO UPDATE SET used=excluded.used',
                            (self.day(), kind, used + amount))

    @contextlib.contextmanager
    def lock(self, name='operation'):
        token = digest([os.getpid(), time.time_ns()])
        try:
            with self.db:
                self.db.execute('INSERT INTO locks VALUES(?,?,?)', (name, os.getpid(), token))
        except sqlite3.IntegrityError as exc:
            raise Stop('Другая операция уже выполняется. После сбоя используйте unlock, проверив PID.') from exc
        try:
            yield
        finally:
            with self.db:
                self.db.execute('DELETE FROM locks WHERE name=? AND token=?', (name, token))

    def topic(self, topic_id):
        row = self.db.execute('SELECT * FROM topics WHERE id=?', (safe_id(topic_id),)).fetchone()
        if not row:
            raise Stop('Тема не найдена; сначала импортируйте очередь.')
        return dict(row)

    def update_topic(self, topic_id, status, error=''):
        with self.db:
            self.db.execute('UPDATE topics SET status=?,updated=?,error=? WHERE id=?', (status, now(), error, topic_id))
        self.event('topic_' + status, topic_id)

    def draft_dir(self, topic_id):
        return within(self.root, 'drafts/' + safe_id(topic_id))
