"""Explicit live capability, bounded requests, public-source fetches, no implicit retry."""
from __future__ import annotations
import ipaddress
import json
import socket
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from .core import Stop, now, validate_url


def public_url(url):
    validate_url(url)
    host = urlsplit(url).hostname
    try:
        addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise Stop('Не удалось разрешить адрес источника.') from exc
    if not addresses or any(not ipaddress.ip_address(x[4][0]).is_global for x in addresses):
        raise Stop('Адрес источника ведёт во внутреннюю/служебную сеть.')


class Redirects(HTTPRedirectHandler):
    def __init__(self,reserve=None):
        super().__init__();self.reserve=reserve

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        public_url(newurl)
        if req.get_method() != 'GET':
            raise Stop('Перенаправление запроса API отклонено.')
        if any(k.lower() in ('authorization', 'x-api-key','x-goog-api-key','cookie') for k in req.headers):
            raise Stop('Перенаправление авторизованного запроса на другой узел отклонено.')
        if self.reserve:self.reserve()
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class HTTP:
    def __init__(self, project, live=False, transport=None):
        self.project, self.live, self.transport = project, live, transport

    def request(self, url, *, method='GET', headers=None, data=None, form=None):
        if not self.live:
            raise Stop('Сетевые обращения выключены. Для согласованного запроса нужен --live.')
        validate_url(url)
        self.project.reserve('requests')
        headers = {'User-Agent': 'SwitchOnAI-SEO-Agents/0.1 (+owner-configured-audit)', **(headers or {})}
        body = None
        if data is not None:
            body = json.dumps(data, ensure_ascii=False).encode('utf-8')
            headers['Content-Type'] = 'application/json'
        elif form is not None:
            from urllib.parse import urlencode
            body = urlencode(form).encode()
            headers['Content-Type'] = 'application/x-www-form-urlencoded'
        try:
            if self.transport:
                result = self.transport(url, method, headers, body)
            else:
                public_url(url)
                req = Request(url, data=body, headers=headers, method=method)
                with build_opener(Redirects(lambda:self.project.reserve('requests'))).open(req, timeout=self.project.config['limits']['http_timeout']) as r:
                    raw = r.read(4_000_001)
                    if len(raw) > 4_000_000:
                        raise Stop('Ответ превышает допустимый размер.')
                    result = {'status': r.status, 'url': r.url, 'headers': dict(r.headers),
                              'text': raw.decode('utf-8', errors='replace')}
            if not 200 <= result['status'] < 300:
                raise Stop(f'Источник вернул HTTP {result["status"]}; данные не приняты.')
            result['received_at'] = now()
            self.project.event('http', data={'host': urlsplit(url).hostname, 'method': method,
                                           'status': result['status']})
            return result
        except HTTPError as exc:
            raise Stop(f'Источник вернул HTTP {exc.code}; проверьте доступ и параметры.') from None
        except (URLError, TimeoutError, OSError) as exc:
            # Never include a URL path/query, credentials or provider response in exceptions.
            raise Stop('Сетевая ошибка; результат запроса неизвестен. Автоматический повтор не выполнен.') from None

    def json(self, url, **kwargs):
        result = self.request(url, **kwargs)
        try:
            return json.loads(result['text'])
        except ValueError as exc:
            raise Stop('Источник вернул ответ, который не является JSON.') from exc
