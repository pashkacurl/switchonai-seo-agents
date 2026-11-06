import datetime as dt
import json
from unittest.mock import patch

from helpers import ProjectCase
from seo_agents.core import Stop, read_json, write_json
from seo_agents.delivery import enqueue, enqueue_text, deliver
from seo_agents.scheduler import activate, pause, schedule_command, tick
from seo_agents.network import HTTP


class DeliverySchedulerTests(ProjectCase):
    def setUp(self):
        super().setUp()
        self.project.config['notifications']['telegram_chat_id']='SYNTHETIC_CHAT'
        self.project.secrets['TELEGRAM_BOT_TOKEN']='SYNTHETIC_TEST_TOKEN'

    def test_delivery_timeout_does_not_retry_automatically(self):
        ids=enqueue_text(self.project,'Synthetic notification','operation1')
        calls=[]
        def transport(*args):calls.append(args);raise TimeoutError('Unknown remote outcome')
        http=HTTP(self.project,True,transport)
        self.assertEqual(deliver(self.project,http)[0]['status'],'unknown')
        self.assertEqual(deliver(self.project,http)[0]['status'],'unknown')
        self.assertEqual(len(calls),1)
        row=self.project.db.execute('SELECT * FROM deliveries WHERE id=?',(ids[0],)).fetchone()
        self.assertEqual(row['attempts'],1)

    def test_delivery_parts_are_acknowledged_individually_and_retry_is_explicit(self):
        ids=enqueue_text(self.project,'x'*6001,'operation1')
        calls=[]
        def transport(url,method,headers,body):
            calls.append(json.loads(body))
            if len(calls)==2:raise TimeoutError('Unknown remote outcome')
            return {'status':200,'url':url,'headers':{},'text':'{"ok":true,"result":{"message_id":42}}'}
        http=HTTP(self.project,True,transport)
        result=deliver(self.project,http)
        self.assertEqual([x['status'] for x in result],['sent','unknown','sent'])
        self.assertEqual(len(ids),3)
        self.assertNotIn('parse_mode',calls[0])
        self.assertEqual(len(deliver(self.project,http)),1)
        self.assertEqual(len(calls),3)
        self.assertEqual(deliver(self.project,http,retry_unknown=True)[0]['status'],'sent')
        self.assertEqual(len(calls),4)
        self.assertEqual(deliver(self.project,http),[])

    def test_offline_delivery_cannot_send(self):
        enqueue_text(self.project,'message','op')
        with self.assertRaises(Stop):deliver(self.project,HTTP(self.project))
        self.assertEqual(self.project.db.execute('SELECT status FROM deliveries').fetchone()[0],'pending')

    def test_activation_requires_real_trial_and_report(self):
        with self.assertRaises(Stop):activate(self.project,content=True)
        with self.assertRaises(Stop):activate(self.project,reports=True)
        self.assertFalse(self.project.config['schedule']['enabled'])

    def test_pause_disables_future_ticks_without_process_control(self):
        self.project.config['schedule']['enabled']=True
        pause(self.project)
        result=tick(self.project,HTTP(self.project,True),None,None)
        self.assertEqual(result,{'status':'paused'})
        self.assertFalse(read_json(self.root/'profile.json')['schedule']['enabled'])
        self.assertEqual(self.project.db.execute('SELECT count(*) FROM runs').fetchone()[0],0)

    def test_schedule_only_returns_command_and_does_not_install_it(self):
        with patch('subprocess.run',side_effect=AssertionError('Process must not start')):
            result=schedule_command(self.project)
        self.assertFalse(result['installed'])
        self.assertIn(str(self.root),result['windows_argv'])

    def test_same_daily_report_does_not_run_twice(self):
        self.project.config['schedule'].update(enabled=True,reports_enabled=True,content_enabled=False,report_hour=8)
        fake_id='fixture-report';directory=self.root/'reports';directory.mkdir()
        (directory/(fake_id+'.md')).write_text('Synthetic report',encoding='utf-8')
        instant=dt.datetime(2026,1,3,10,tzinfo=self.project.tz)
        with patch('seo_agents.monitoring.collect',return_value=(fake_id,{})) as collect_mock, \
             patch('seo_agents.delivery.deliver',return_value=[]):
            tick(self.project,HTTP(self.project,True),None,None,clock=instant)
            tick(self.project,HTTP(self.project,True),None,None,clock=instant)
        self.assertEqual(collect_mock.call_count,1)
