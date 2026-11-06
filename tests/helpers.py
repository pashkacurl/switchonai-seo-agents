"""Synthetic offline fixtures. No credentials, DNS, models or deployments."""
import csv
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from seo_agents.core import Project, initialize, now, read_json, write_json


SOURCE = 'https://source.example.test/evidence'
COMPETITOR = 'https://competitor.example.test/article'
FACT = 'The service accepts requests through a contact form.'


def topic(**overrides):
    row = dict(topic_id='T1', cluster_id='C1', page_id='P1', slug='sample-topic',
               title='Sample topic', primary_query='sample query', query_ids='Q1',
               category='Guides', source_urls=SOURCE, serp_urls=COMPETITOR,
               evidence_status='confirmed', serp_run_id='SERP1', kind='new')
    row.update(overrides)
    return row


def write_csv(path, rows):
    fields = list(dict.fromkeys(key for row in rows for key in row))
    with Path(path).open('w', encoding='utf-8', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader(); writer.writerows(rows)
    return path


def article():
    return dict(title='Sample topic', description='A useful explanation of a synthetic service.',
                markdown='## How it works\n\n' + FACT + '\n\n'
                    'Review the available details, explain your question and keep a copy of the request.\n',
                claims=[dict(claim=FACT, evidence_id='E1', quote=FACT)], media=[], changes='')


def research_data():
    return {'at': now(), 'topic': topic(), 'sources': [
        {'evidence_id': 'E1', 'url': SOURCE, 'final_url': SOURCE, 'text': FACT,
         'retrieved_at': now()}], 'competitors': [], 'serp_run_id': 'SERP1'}


class ProjectCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='seo-agents-test-')
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / 'private profile with spaces'
        initialize(self.root)
        config = read_json(self.root / 'profile.json')
        config['site']['url'] = 'https://site.example.test'
        config['site']['author'] = 'Synthetic editorial team'
        config['sources']['approved_urls'] = [SOURCE]
        config['limits'].update(requests_per_day=100, articles_per_day=5,
                                model_calls_per_day=10, output_tokens_per_day=60000)
        write_json(self.root / 'profile.json', config)
        self.project = Project(self.root)
        self.addCleanup(self.project.close)

    def import_rows(self, rows=None):
        from seo_agents.content import import_topics
        path = write_csv(self.root / 'topics.csv', rows or [topic()])
        return import_topics(self.project, path)

    def save_research(self, topic_id='T1'):
        write_json(self.project.draft_dir(topic_id) / 'research.json', research_data())
        self.project.update_topic(topic_id, 'researched')
