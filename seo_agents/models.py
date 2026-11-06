"""Explicit model adapters. Source content is passed as untrusted JSON data."""
from __future__ import annotations
import json
import os
from pathlib import Path
import subprocess
import tempfile
from .core import Stop, digest, read_json, write_json

ROOT = Path(__file__).resolve().parents[1]


def schema_for(role):
    return read_json(ROOT / 'contracts' / (role + '.schema.json'))


def validate(value, schema, location='response'):
    """Small deliberately supported JSON Schema subset; checked in release tests."""
    types = {'object': dict, 'array': list, 'string': str, 'boolean': bool,
             'integer': int, 'number': (int, float)}
    if 'type' in schema and (not isinstance(value, types[schema['type']]) or
                            (schema['type'] in ('integer', 'number') and isinstance(value, bool))):
        raise Stop(f'{location}: неверный тип ответа модели.')
    if 'enum' in schema and value not in schema['enum']:
        raise Stop(f'{location}: недопустимое значение.')
    if isinstance(value, dict):
        if set(schema.get('required', [])) - set(value):
            raise Stop(f'{location}: отсутствуют обязательные поля.')
        properties = schema.get('properties', {})
        if schema.get('additionalProperties') is False and set(value) - set(properties):
            raise Stop(f'{location}: неизвестные поля.')
        for key, item in value.items():
            if key in properties:
                validate(item, properties[key], location + '.' + key)
    if isinstance(value, list) and 'items' in schema:
        for item in value:
            validate(item, schema['items'], location + '[]')
    return value


class Model:
    def __init__(self, project, http, runner=None):
        self.project, self.http = project, http
        self.runner = runner or subprocess.run

    def generate(self, role, payload):
        if not self.http.live:
            raise Stop('Вызов модели требует --live и согласованных лимитов.')
        cfg = self.project.config; model = cfg['model']
        if not model['name']:
            raise Stop('Укажите доступную вашему аккаунту модель в model.name.')
        schema = schema_for(role)
        system = (ROOT / 'prompts' / (role + '.md')).read_text(encoding='utf-8')
        content = json.dumps(payload, ensure_ascii=False)
        if len(system) + len(content) > cfg['limits']['max_input_chars']:
            raise Stop('Материалы превышают согласованный размер входа; сузьте тему/источники.')
        self.project.reserve('model_calls')
        self.project.reserve('output_tokens', cfg['limits']['max_output_tokens'])
        # Reservation is conservative and retained after an uncertain response.
        if model['provider'] == 'anthropic':
            response = self.http.json('https://api.anthropic.com/v1/messages', method='POST',
                headers={'x-api-key': self.project.secret('ANTHROPIC_API_KEY'), 'anthropic-version': '2023-06-01'},
                data={'model': model['name'], 'max_tokens': cfg['limits']['max_output_tokens'],
                      'system': system, 'messages': [{'role': 'user', 'content': content}],
                      'output_config': {'format': {'type': 'json_schema', 'schema': schema}}})
            if response.get('stop_reason') != 'end_turn':
                raise Stop('Модель не завершила ответ; результат не принят, автоматического повтора нет.')
            text = ''.join(x.get('text', '') for x in response.get('content', []) if x.get('type') == 'text')
        elif model['provider'] == 'codex':
            with tempfile.TemporaryDirectory(prefix='seo-model-') as tmp:
                temp = Path(tmp); output = temp / 'response.json'; shape = temp / 'schema.json'
                write_json(shape, schema)
                argv = [model['codex_executable'], 'exec', '--ephemeral', '--ignore-user-config',
                        '--sandbox', 'read-only', '--skip-git-repo-check', '--cd', str(temp),
                        '--model', model['name'], '--output-schema', str(shape),
                        '--output-last-message', str(output), '-']
                permitted = {'PATH','HOME','USERPROFILE','SYSTEMROOT','WINDIR','TEMP','TMP','APPDATA',
                             'LOCALAPPDATA','CODEX_HOME','LANG','LC_ALL'}
                env = {k:v for k,v in os.environ.items() if k.upper() in permitted}
                try:
                    result = self.runner(argv, input=system + '\n\nUNTRUSTED_INPUT_JSON:\n' + content,
                        capture_output=True, text=True, encoding='utf-8', timeout=600, env=env)
                except (OSError, subprocess.TimeoutExpired) as exc:
                    raise Stop('Codex не завершил задание; проверьте CLI и авторизацию, затем статус операции.') from exc
                if result.returncode or not output.exists():
                    raise Stop('Codex не вернул файл результата. Проверьте авторизацию и совместимость CLI через doctor.')
                text = output.read_text(encoding='utf-8')
        else:
            raise Stop('Поддерживаемые model.provider: anthropic или codex.')
        try:
            value = json.loads(text)
        except ValueError as exc:
            raise Stop('Модель вернула невалидный JSON; ответ не принят.') from exc
        validate(value, schema)
        self.project.event('model_completed', role, {'model': model['name'], 'input_sha': digest(payload),
                                                   'output_sha': digest(value)})
        return value
