import copy
import json
import socket
from unittest.mock import patch
from urllib.request import Request

from helpers import ProjectCase, SOURCE, COMPETITOR, article, topic, research_data
from seo_agents.core import Stop, initialize, safe_id, safe_slug, validate_url, within, read_json
from seo_agents.content import check_article, import_topics, research, materialize
from seo_agents.network import HTTP, public_url, Redirects
from seo_agents.models import validate


class CoreTests(ProjectCase):
    def test_init_does_not_overwrite_existing_profile_or_secret(self):
        (self.root / '.env').write_text('TEST_PRIVATE_VALUE=leave-this\n')
        before = (self.root / 'profile.json').read_bytes()
        with self.assertRaises(Stop): initialize(self.root)
        self.assertEqual(before, (self.root / 'profile.json').read_bytes())
        self.assertIn('leave-this', (self.root / '.env').read_text())
        self.assertIn('.env', (self.root / '.gitignore').read_text().splitlines())

    def test_safe_identifiers_and_paths(self):
        for value in ('../secret', '/root', '', 'a/b', 'a\\b', 'a' * 101):
            with self.subTest(value=value), self.assertRaises(Stop): safe_id(value)
        for value in ('../article', 'Title', 'a--b', 'a/b'):
            with self.subTest(value=value), self.assertRaises(Stop): safe_slug(value)
        with self.assertRaises(Stop): within(self.root, '../outside.txt')
        with self.assertRaises(Stop): within(self.root, '.')

    def test_no_network_in_offline_or_exhausted_budget(self):
        with patch('socket.getaddrinfo', side_effect=AssertionError('DNS called')):
            with self.assertRaises(Stop): HTTP(self.project).request(SOURCE)
            self.project.config['limits']['requests_per_day'] = 0
            with self.assertRaises(Stop): HTTP(self.project, live=True).request(SOURCE)

    def test_failed_transport_keeps_conservative_budget_without_retry(self):
        calls = []
        def transport(*args):
            calls.append(args); raise TimeoutError('sensitive-token')
        with self.assertRaisesRegex(Stop, 'Сетевая ошибка') as caught:
            HTTP(self.project, live=True, transport=transport).request(SOURCE)
        self.assertNotIn('sensitive-token', str(caught.exception))
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.project.db.execute('SELECT used FROM budgets WHERE kind="requests"').fetchone()[0], 1)

    def test_nested_operation_is_excluded_and_released(self):
        with self.project.lock():
            with self.assertRaises(Stop):
                with self.project.lock(): self.fail('Lock was bypassed')
        with self.project.lock(): pass

    def test_url_blocks_credentials_http_fragment_and_private_dns(self):
        for url in ('http://site.test', 'https://u:p@site.test', 'https://site.test:444/', 'https://site.test/#x'):
            with self.subTest(url=url), self.assertRaises(Stop): validate_url(url)
        with patch('socket.getaddrinfo', return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1',443))]):
            with self.assertRaises(Stop): public_url('https://example.test/')

    def test_schema_rejects_unknown_properties_bool_as_number_and_missing_field(self):
        schema = {'type':'object', 'required':['count'], 'additionalProperties':False,
                  'properties':{'count':{'type':'integer'}}}
        for value in ({'count':True}, {}, {'count':1, 'surprise':'x'}):
            with self.subTest(value=value), self.assertRaises(Stop): validate(value,schema)
        self.assertEqual(validate({'count':1},schema), {'count':1})

    def test_authorized_requests_cannot_forward_headers_through_redirect(self):
        for header in ('Authorization','X-Api-Key','X-Goog-Api-Key','Cookie'):
            req=Request(SOURCE,headers={header:'SYNTHETIC_TEST_SECRET'})
            with self.subTest(header=header),patch('seo_agents.network.public_url'),self.assertRaises(Stop):
                Redirects().redirect_request(req,None,302,'redirect',{},'https://other.example.test/')


class ContentTests(ProjectCase):
    def test_conflicting_query_ownership_is_atomic(self):
        self.import_rows()
        with self.assertRaises(Stop):
            self.import_rows([topic(topic_id='T2',cluster_id='C2',page_id='P2',slug='another',query_ids='Q2'),
                              topic(topic_id='T3',cluster_id='C3',page_id='P3',slug='conflict')])
        self.assertEqual(self.project.db.execute('SELECT count(*) FROM topics').fetchone()[0],1)

    def test_identical_import_idempotent_changed_payload_refused(self):
        self.import_rows(); self.import_rows()
        self.assertEqual(self.project.db.execute('SELECT count(*) FROM topics').fetchone()[0],1)
        with self.assertRaises(Stop): self.import_rows([topic(title='Changed')])

    def test_hypothesis_never_fetches_or_enters_ready_queue(self):
        self.import_rows([topic(evidence_status='hypothesis')])
        class NoHTTP:
            def request(self,*args): raise AssertionError('Unexpected fetch')
        with self.assertRaises(Stop): research(self.project,'T1',NoHTTP())
        self.assertEqual(self.project.topic('T1')['status'],'needs_research')

    def test_unapproved_redirect_is_not_research(self):
        self.import_rows()
        def transport(*args):
            return {'status':200,'url':'https://new-source.example.test/', 'headers':{},
                    'text':'<p>'+('Useful evidence is present. '*10)+'</p>'}
        with self.assertRaisesRegex(Stop,'перенаправил'):
            research(self.project,'T1',HTTP(self.project,True,transport))
        self.assertFalse((self.project.draft_dir('T1')/'research.json').exists())

    def test_fabricated_quote_is_refused(self):
        value=article();value['claims'][0]['quote']='This is not present in the evidence.'
        with self.assertRaises(Stop):check_article(value,research_data(),self.project)

    def test_unsafe_markdown_is_refused(self):
        for suffix in ('\n<script>alert(1)</script>', '\nexport const x = 1',
                       '\n{ process.exit() }', '\n{\nprocess.exit()\n}',
                       '\n[unsafe](javascript:alert)', '\n[x][bad]\n\n[bad]: https://unapproved.example.test/'):
            value=article();value['markdown']+=suffix
            with self.subTest(suffix=suffix),self.assertRaises(Stop):
                check_article(value,research_data(),self.project)

    def test_media_alt_cannot_inject_mdx(self):
        value=article();value['media']=[{'type':'steps','items':['One','Two'],
                                      'alt':'diagram\n{\nprocess.exit()\n}'}]
        with self.assertRaises(Stop):check_article(value,research_data(),self.project)

    def test_refresh_can_target_its_original_owned_page(self):
        self.import_rows();self.project.update_topic('T1','published')
        self.import_rows([topic(topic_id='T1-refresh',kind='refresh',original_date='2026-01-01',
                                previous_sha256='a'*64)])
        self.assertEqual(self.project.topic('T1-refresh')['kind'],'refresh')

    def test_materialization_has_hashes_and_real_author(self):
        self.import_rows();value=article()
        value['media']=[{'type':'steps','items':['First action','Second action'],'alt':'Two actions'}]
        check_article(value,research_data(),self.project)
        bundle=materialize(self.project,'T1',value)
        self.assertEqual(len(bundle['operation_id']),40)
        self.assertEqual(len(bundle['content_sha256']),64)
        text=(self.project.draft_dir('T1')/'article.mdx').read_text(encoding='utf-8')
        self.assertIn('Synthetic editorial team',text)
        self.assertIn('seo_revision:',text)
        self.assertEqual(len(bundle['media_sha256']),1)
