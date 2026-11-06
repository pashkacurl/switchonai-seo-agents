import copy
import datetime as dt
import json
from unittest.mock import patch

from helpers import ProjectCase, SOURCE, article, topic, research_data, write_csv
from seo_agents.core import Stop, digest, now, read_json, write_json
from seo_agents.content import materialize
from seo_agents.pipeline import approve, draft, fresh_research, Publisher
from seo_agents.monitoring import measurements, collect
from seo_agents.network import HTTP


class PipelineTests(ProjectCase):
    def setUp(self):
        super().setUp();self.import_rows();self.save_research()
        (self.root/'business.md').write_text('This synthetic business uses a contact form. '
            'Its actual owner reviews the factual information and decides which sources can be used. '*2,encoding='utf-8')

    def ready(self):
        bundle=materialize(self.project,'T1',article());self.project.update_topic('T1','review')
        approve(self.project,'T1');return bundle

    def test_changed_article_cannot_be_approved(self):
        materialize(self.project,'T1',article());self.project.update_topic('T1','review')
        (self.project.draft_dir('T1')/'article.mdx').write_text('Altered after review',encoding='utf-8')
        with self.assertRaises(Stop):approve(self.project,'T1')
        self.assertEqual(self.project.topic('T1')['status'],'review')

    def test_stale_or_revoked_sources_block_work(self):
        data=research_data();data['at']=(dt.datetime.now(dt.timezone.utc)-dt.timedelta(days=60)).isoformat()
        write_json(self.project.draft_dir('T1')/'research.json',data)
        with self.assertRaises(Stop):fresh_research(self.project,'T1')
        write_json(self.project.draft_dir('T1')/'research.json',research_data())
        self.project.config['sources']['approved_urls']=[]
        with self.assertRaises(Stop):fresh_research(self.project,'T1')

    def test_draft_retries_only_unfinished_model_stage(self):
        calls=[]
        class Model:
            fail=True
            def generate(self,role,payload):
                calls.append(role)
                if role=='brief':return {'purpose':'Synthetic brief'}
                if role=='author':return article()
                if self.fail:
                    self.fail=False;raise Stop('Controlled model failure')
                return {'verdict':'accept','issues':[],'article':article()}
        model=Model()
        with self.assertRaises(Stop):draft(self.project,'T1',model)
        self.assertEqual(self.project.topic('T1')['status'],'blocked')
        draft(self.project,'T1',model)
        self.assertEqual(calls,['brief','author','editor','editor'])
        self.assertEqual(self.project.topic('T1')['status'],'review')

    def test_changed_inputs_never_reuse_cached_model_output(self):
        class FailModel:
            def generate(self,role,payload):raise Stop('Controlled failure')
        with self.assertRaises(Stop):draft(self.project,'T1',FailModel())
        self.project.config['site']['author']='Changed editorial owner'
        with self.assertRaisesRegex(Stop,'Входы изменились'):draft(self.project,'T1',FailModel())

    def test_publish_needs_both_live_and_publish_permission(self):
        self.ready()
        class NoSite:
            def dispatch(self,request):raise AssertionError('Site must not be contacted')
        for live,allowed in ((False,False),(False,True),(True,False)):
            publisher=Publisher(self.project,HTTP(self.project,live=live),site=NoSite())
            with self.subTest(live=live,allowed=allowed),self.assertRaises(Stop):
                publisher.publish('T1',allow_publish=allowed)
        self.assertEqual(self.project.topic('T1')['status'],'approved')

    def test_uncertain_site_receipt_never_repeats_publish_or_marks_success(self):
        self.ready();calls=[]
        class Site:
            def dispatch(self,request):
                calls.append(request['action'])
                return {'status':'uncertain','operation_id':request['operation_id']}
        publisher=Publisher(self.project,HTTP(self.project,live=True),site=Site())
        for _ in range(2):
            with self.assertRaises(Stop):publisher.publish('T1',allow_publish=True)
        self.assertEqual(calls,['status','status'])
        self.assertEqual(self.project.topic('T1')['status'],'publishing')
        self.assertEqual(self.project.db.execute('SELECT used FROM budgets WHERE kind="articles"').fetchone()[0],1)

    def test_accepted_receipt_is_not_enough_without_public_url_checks(self):
        bundle=self.ready()
        class Site:
            def dispatch(self,request):return {'status':'accepted','operation_id':bundle['operation_id'],'bundle':bundle}
        def transport(url,*args):return {'status':200,'url':url,'headers':{},'text':'<h1>Wrong page</h1>'}
        publisher=Publisher(self.project,HTTP(self.project,True,transport),site=Site())
        with self.assertRaises(Stop):publisher.publish('T1',allow_publish=True)
        self.assertEqual(self.project.topic('T1')['status'],'publishing')


class MeasurementTests(ProjectCase):
    def row(self,**updates):
        value=dict(query_id='Q1',source_run_id='R1',source='synthetic_serp',engine='google',
            region='GB',device='desktop',depth='100',metric='serp_position',date_start='2026-01-01',
            date_end='2026-01-01',position='7',result_status='observed',target_url='https://site.example.test/blog/test/')
        value.update(updates);return value

    def run_rows(self,rows):
        path=write_csv(self.root/'measurements.csv',rows)
        self.project.config['sources']['measurements_import']=str(path)
        return measurements(self.project)

    def test_multiple_measurements_preserved_and_only_comparable_rows_compared(self):
        result=self.run_rows([self.row(),self.row(source_run_id='R2',date_start='2026-01-02',date_end='2026-01-02',position='10'),
            self.row(source_run_id='R3',date_start='2026-01-03',date_end='2026-01-03',region='US',position='1')])
        self.assertEqual(len(result['rows']),3)
        self.assertEqual(len(result['changes']),1)
        self.assertEqual(result['changes'][0]['delta'],3)

    def test_missing_position_is_unknown_not_zero(self):
        result=self.run_rows([self.row(),self.row(source_run_id='R2',date_start='2026-01-02',date_end='2026-01-02',
                                               position='',result_status='unknown')])
        self.assertNotIn('delta',result['changes'][0])
        self.assertEqual(result['changes'][0]['status'],'unknown')

    def test_duplicate_observation_and_nonfinite_position_rejected(self):
        with self.assertRaises(Stop):self.run_rows([self.row(),self.row()])
        with self.assertRaises(Stop):self.run_rows([self.row(position='NaN')])

    def test_offline_report_does_not_invent_success_or_zero_metrics(self):
        with patch('socket.getaddrinfo',side_effect=AssertionError('Unexpected network')):
            report_id,result=collect(self.project,HTTP(self.project),weekly=True)
        self.assertTrue((self.root/'reports'/(report_id+'.json')).exists())
        self.assertTrue(all(x['status']=='Не проверено' for x in result['modules'].values()))
        self.assertEqual(result['candidates'],[])
        self.assertEqual(result['priorities'],[])

    def test_malformed_provider_dimensions_do_not_destroy_entire_report(self):
        incomplete={'source':'gsc','rows':[{'keys':['query only'],'impressions':10}],
                    'start':'2026-01-01','end':'2026-01-07'}
        with patch('seo_agents.providers.gsc',return_value=incomplete):
            report_id,result=collect(self.project,HTTP(self.project))
        self.assertTrue((self.root/'reports'/(report_id+'.json')).exists())
        self.assertEqual(result['modules']['gsc']['status'],'Не проверено')
        self.assertEqual(result['candidates'],[])
