import contextlib
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import zipfile
from unittest.mock import patch

from helpers import ProjectCase, ROOT
from seo_agents.cli import main
from seo_agents.core import ENV_NAMES, read_json
from seo_agents.models import schema_for


class CLITests(ProjectCase):
    def cli(self,*args,project=None):
        env={k:v for k,v in os.environ.items() if k not in ENV_NAMES}
        return subprocess.run([sys.executable,'-X','utf8','-B',str(ROOT/'seo.py'),
            '--project',str(project or self.root),*args],cwd=self.base,capture_output=True,
            text=True,encoding='utf-8',env=env,timeout=30)

    def test_help_works_outside_repository(self):
        result=self.cli('--help')
        self.assertEqual(result.returncode,0,result.stderr)
        for command in ('init','doctor','publish','rollback','demo','backup'):
            self.assertIn(command,result.stdout)

    def test_init_doctor_status_in_path_with_spaces_and_cyrillic(self):
        profile=self.base/'новый профиль с пробелами'
        result=self.cli('init',project=profile)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertTrue((profile/'profile.json').exists())
        result=self.cli('doctor',project=profile)
        self.assertEqual(result.returncode,0,result.stderr)
        data=json.loads(result.stdout)
        self.assertEqual(data['status'],'needs_setup')
        self.assertEqual(data['network_requests'],0)
        result=self.cli('status',project=profile)
        self.assertEqual(result.returncode,0,result.stderr)
        data=json.loads(result.stdout)
        self.assertEqual(data['topics'],[])
        self.assertFalse(data['schedule']['enabled'])

    def test_demo_is_offline_and_does_not_mutate_selected_profile(self):
        before=(self.root/'profile.json').read_bytes()
        result=self.cli('demo')
        self.assertEqual(result.returncode,0,result.stderr)
        data=json.loads(result.stdout)
        self.assertEqual(data['mode'],'OFFLINE_SYNTHETIC_DEMO')
        self.assertEqual(data['network_requests'],0)
        self.assertEqual(data['model_calls'],0)
        self.assertEqual(data['real_publications'],0)
        self.assertEqual(before,(self.root/'profile.json').read_bytes())
        self.assertEqual(self.project.db.execute('SELECT count(*) FROM topics').fetchone()[0],0)

    def test_working_report_without_live_flag_is_rejected(self):
        result=self.cli('report')
        self.assertEqual(result.returncode,2)
        self.assertIn('--live',json.loads(result.stderr)['message'])
        self.assertFalse((self.root/'reports').exists())

    def test_backup_contains_consistent_sqlite_but_no_env_or_wal(self):
        self.import_rows();self.project.reserve('requests')
        (self.root/'.env').write_text('ANTHROPIC_API_KEY=SYNTHETIC_SECRET_NOT_IN_BACKUP',encoding='utf-8')
        output=self.base/'private backup.zip'
        result=self.cli('backup',str(output))
        self.assertEqual(result.returncode,0,result.stderr)
        with zipfile.ZipFile(output) as archive:
            self.assertNotIn('.env',archive.namelist())
            self.assertNotIn('state/state.sqlite3-wal',archive.namelist())
            self.assertNotIn('state/state.sqlite3-shm',archive.namelist())
            self.assertFalse(any(b'SYNTHETIC_SECRET_NOT_IN_BACKUP' in archive.read(name) for name in archive.namelist()))
            copy=self.base/'restored-state.sqlite3';copy.write_bytes(archive.read('state/state.sqlite3'))
        db=sqlite3.connect(copy)
        try:
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0],'ok')
            self.assertEqual(db.execute('SELECT id FROM topics').fetchone()[0],'T1')
            self.assertEqual(db.execute('SELECT used FROM budgets WHERE kind="requests"').fetchone()[0],1)
        finally:db.close()

    def test_every_model_role_has_readable_schema_and_prompt(self):
        for role in ('brief','author','editor','strategist'):
            with self.subTest(role=role):
                schema=schema_for(role)
                self.assertEqual(schema['type'],'object')
                self.assertTrue(schema.get('required'))
                self.assertFalse(schema['additionalProperties'])
                self.assertGreater(len((ROOT/'prompts'/(role+'.md')).read_text(encoding='utf-8')),100)

    def test_unlock_does_not_signal_a_live_windows_process(self):
        if os.name!='nt':self.skipTest('Windows-specific no-TerminateProcess regression')
        with self.project.db:
            self.project.db.execute('INSERT INTO locks VALUES(?,?,?)',('operation',os.getpid(),'test-lock'))
        output=io.StringIO()
        with patch('os.kill',side_effect=AssertionError('Windows PID lookup must not call os.kill')), \
             contextlib.redirect_stderr(output),contextlib.redirect_stdout(output):
            code=main(['--project',str(self.root),'unlock','--pid',str(os.getpid())])
        self.assertEqual(code,2)
        self.assertEqual(self.project.db.execute('SELECT count(*) FROM locks').fetchone()[0],1)
