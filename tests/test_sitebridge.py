import copy
import os
from pathlib import Path
import shutil
import subprocess

from helpers import ProjectCase, article
from seo_agents.content import materialize
from seo_agents.core import Stop, digest, read_json, write_json
from seo_agents.sitebridge import Site, verify_release
from seo_agents.network import HTTP
from seo_agents.pipeline import draft, approve, Publisher


class SiteBridgeTests(ProjectCase):
    def setUp(self):
        super().setUp()
        self.import_rows()
        self.bundle = materialize(self.project, 'T1', article())
        self.checkout = self.base / 'synthetic site'
        self.checkout.mkdir()
        self.site_config = dict(checkout=str(self.checkout), content_dir='content/blog', public_dir='public',
            state_dir=str(self.base/'site receipts'), site_url=self.project.config['site']['url'], blog_path='/blog/',
            build_command=['fixture-build'], deploy_command=['fixture-deploy'], rollback_command=['fixture-rollback'],
            acceptance_command=['fixture-acceptance'], timeout_seconds=30)
        self.config_path=self.base/'site-profile.json';write_json(self.config_path,self.site_config)
        self.calls=[]
        self.fail_command=''
        self.site=Site(self.config_path,runner=self.run_command,fetch=self.fetch)
        self.request=dict(action='publish',operation_id=self.bundle['operation_id'],bundle=self.bundle,
            files={'article.mdx':(self.project.draft_dir('T1')/'article.mdx').read_text(encoding='utf-8')})

    def run_command(self,argv,**kwargs):
        self.calls.append(argv[0])
        return subprocess.CompletedProcess(argv, 1 if argv[0]==self.fail_command else 0, stdout='', stderr='')

    def fetch(self,url):
        if url.endswith('/robots.txt'):
            text='User-agent: *\nAllow: /\n'
        elif url.endswith('/sitemap.xml'):
            text='<urlset><url><loc>'+self.bundle['url']+'</loc></url></urlset>'
        elif url==self.bundle['url']:
            text=('<html><head><title>'+self.bundle['title']+'</title>'
                  '<link rel="canonical" href="'+url+'">'
                  '<meta name="seo-revision" content="'+self.bundle['revision']+'">'
                  '</head><body><h1>'+self.bundle['title']+'</h1><p>Fixture article</p></body></html>')
        else:
            raise AssertionError('Unexpected synthetic fetch: '+url)
        return dict(status=200,url=url,headers={},text=text)

    def test_release_writes_only_allowed_files_and_is_idempotent(self):
        receipt=self.site.dispatch(self.request)
        self.assertEqual(receipt['status'],'accepted')
        self.assertNotIn('backups',receipt)
        self.assertEqual(self.calls,['fixture-build','fixture-acceptance','fixture-deploy'])
        self.assertEqual(self.site.dispatch(self.request)['status'],'accepted')
        self.assertEqual(self.calls.count('fixture-deploy'),1)
        path=self.checkout/'content/blog/sample-topic.mdx'
        self.assertEqual(digest(path.read_bytes()),self.bundle['content_sha256'])

    def test_failed_build_restores_files_and_never_deploys(self):
        self.fail_command='fixture-build'
        with self.assertRaises(Stop):self.site.dispatch(self.request)
        self.assertFalse((self.checkout/'content/blog/sample-topic.mdx').exists())
        self.assertNotIn('fixture-deploy',self.calls)
        result=self.site.dispatch(dict(action='status',operation_id=self.bundle['operation_id']))
        self.assertEqual(result['status'],'failed')

    def test_unknown_deploy_is_reconciled_without_redeployment(self):
        self.fail_command='fixture-deploy'
        with self.assertRaises(Stop):self.site.dispatch(self.request)
        receipt_path=self.base/'site receipts'/(self.bundle['operation_id']+'.json')
        self.assertEqual(read_json(receipt_path)['status'],'uncertain')
        self.assertEqual(self.site.dispatch(self.request)['status'],'uncertain')
        self.assertEqual(self.calls.count('fixture-deploy'),1)
        self.fail_command=''
        status=self.site.dispatch(dict(action='status',operation_id=self.bundle['operation_id']))
        self.assertEqual(status['status'],'accepted')
        self.assertEqual(self.calls.count('fixture-deploy'),1)

    def test_tampered_article_rejected_before_any_write(self):
        self.request['files']['article.mdx']+='Tampered text'
        with self.assertRaises(Stop):self.site.dispatch(self.request)
        self.assertEqual(self.calls,[])
        self.assertFalse((self.checkout/'content').exists())

    def test_foreign_frontmatter_and_multiline_mdx_rejected_even_with_matching_hash(self):
        for suffix in ('\n{\nprocess.exit()\n}',):
            request=copy.deepcopy(self.request)
            request['files']['article.mdx']+=suffix
            request['bundle']['content_sha256']=digest(request['files']['article.mdx'].encode())
            with self.assertRaises(Stop):self.site.dispatch(request)
        request=copy.deepcopy(self.request)
        request['files']['article.mdx']=request['files']['article.mdx'].replace('draft: false','danger: "x"\ndraft: false')
        request['bundle']['content_sha256']=digest(request['files']['article.mdx'].encode())
        with self.assertRaises(Stop):self.site.dispatch(request)
        self.assertEqual(self.calls,[])

    def test_rollback_preserves_later_owner_changes(self):
        self.site.dispatch(self.request)
        path=self.checkout/'content/blog/sample-topic.mdx'
        path.write_text('OWNER EDIT MUST SURVIVE',encoding='utf-8')
        with self.assertRaises(Stop):self.site.dispatch(dict(action='rollback',operation_id=self.bundle['operation_id']))
        self.assertEqual(path.read_text(),'OWNER EDIT MUST SURVIVE')
        self.assertNotIn('fixture-rollback',self.calls)

    def test_rollback_restores_created_file_and_is_idempotent(self):
        self.site.dispatch(self.request)
        request=dict(action='rollback',operation_id=self.bundle['operation_id'])
        self.assertEqual(self.site.dispatch(request)['status'],'rolled_back')
        self.assertFalse((self.checkout/'content/blog/sample-topic.mdx').exists())
        self.assertEqual(self.site.dispatch(request)['status'],'rolled_back')
        self.assertEqual(self.calls.count('fixture-rollback'),1)

    def test_refresh_with_changed_base_does_not_overwrite(self):
        path=self.checkout/'content/blog/sample-topic.mdx';path.parent.mkdir(parents=True)
        path.write_text('LATEST OWNER CONTENT',encoding='utf-8')
        self.request['bundle']['kind']='refresh';self.request['bundle']['previous_sha256']='a'*64
        with self.assertRaises(Stop):self.site.dispatch(self.request)
        self.assertEqual(path.read_text(),'LATEST OWNER CONTENT')
        self.assertEqual(self.calls,[])

    def test_refresh_rollback_preserves_original_crlf_bytes(self):
        path=self.checkout/'content/blog/sample-topic.mdx';path.parent.mkdir(parents=True)
        original=b'Previous content\r\nSecond line\r\n'
        path.write_bytes(original)
        self.request['bundle']['kind']='refresh'
        self.request['bundle']['previous_sha256']=digest(original)
        self.site.dispatch(self.request)
        self.site.dispatch(dict(action='rollback',operation_id=self.bundle['operation_id']))
        self.assertEqual(path.read_bytes(),original)

    def test_model_to_receipt_chain_queues_notifications_without_sending(self):
        self.save_research()
        (self.root/'business.md').write_text('This is a synthetic business with real facts in its test fixture. '
            'The owner accepts requests through a contact form and reviews materials before publishing. '*2,encoding='utf-8')
        roles=[]
        class Model:
            def generate(self,role,payload):
                roles.append(role)
                if role=='brief':return {'purpose':'Synthetic brief'}
                if role=='author':return article()
                return {'verdict':'accept','issues':[],'article':article()}
        self.bundle=draft(self.project,'T1',Model())
        approve(self.project,'T1')
        self.project.config['notifications']['telegram_chat_id']='SYNTHETIC_CHAT'
        transport=lambda url,*args:self.fetch(url)
        publisher=Publisher(self.project,HTTP(self.project,True,transport),site=self.site)
        result=publisher.publish('T1',allow_publish=True)
        self.assertEqual(result['status'],'accepted')
        self.assertEqual(roles,['brief','author','editor'])
        self.assertEqual(self.project.topic('T1')['status'],'published')
        self.assertEqual(self.project.db.execute('SELECT status FROM deliveries').fetchone()[0],'pending')
        publisher.publish('T1',allow_publish=True)
        self.assertEqual(self.calls.count('fixture-deploy'),1)
        self.assertEqual(self.project.db.execute('SELECT count(*) FROM deliveries').fetchone()[0],1)

    def test_release_verification_checks_indexing_revision_canonical_and_sitemap(self):
        mutations=[
            lambda result:result.update(status=404),
            lambda result:result.update(text=result['text'].replace('rel="canonical"','rel="alternate"')),
            lambda result:result.update(text=result['text'].replace(self.bundle['revision'],'badrevision')),
            lambda result:result.update(headers={'x-robots-tag':'noindex'}),
            lambda result:result.update(text=result['text']+'<meta name="robots" content="noindex">'),
        ]
        for mutate in mutations:
            def fetch(url):
                result=self.fetch(url)
                if url==self.bundle['url']:mutate(result)
                return result
            with self.subTest(mutation=mutate),self.assertRaises(Stop):
                verify_release(self.site_config,self.bundle,fetch)
        def empty_sitemap(url):
            result=self.fetch(url)
            if url.endswith('/sitemap.xml'):result['text']='<urlset />'
            return result
        with self.assertRaises(Stop):verify_release(self.site_config,self.bundle,empty_sitemap)

    def test_content_or_media_paths_cannot_escape_checkout(self):
        cfg=copy.deepcopy(self.site_config);cfg['content_dir']='../elsewhere'
        write_json(self.config_path,cfg)
        with self.assertRaises(Stop):Site(self.config_path,runner=self.run_command,fetch=self.fetch)
        request=copy.deepcopy(self.request);request['bundle']['media']=['../attack.svg']
        with self.assertRaises(Stop):self.site.dispatch(request)
        self.assertEqual(self.calls,[])

    def test_real_git_commit_dirty_tree_guard_and_rollback_in_synthetic_fixture(self):
        git=shutil.which('git')
        if not git:self.skipTest('Git is optional for file-only site adapter')
        # One new synthetic repository in TemporaryDirectory; no clone, remote,
        # global configuration, hooks, shell, live build or deployment.
        env=os.environ.copy()
        env.update(GIT_CONFIG_NOSYSTEM='1',GIT_CONFIG_GLOBAL=os.devnull)
        env.pop('GIT_DIR',None);env.pop('GIT_WORK_TREE',None);env.pop('GIT_INDEX_FILE',None)
        def git_run(argv,**kwargs):
            return subprocess.run([git,'-c','safe.directory='+str(self.checkout),*argv],
                cwd=self.checkout,capture_output=True,text=True,encoding='utf-8',env=env,timeout=20)
        for argv in (['init','--initial-branch=main'],['config','user.name','Synthetic Test'],
                     ['config','user.email','synthetic@example.test'],['config','commit.gpgsign','false'],
                     ['config','core.autocrlf','false'],['config','core.hooksPath',str(self.base/'no-hooks')]):
            result=git_run(argv);self.assertEqual(result.returncode,0,result.stderr)
        (self.checkout/'README.md').write_bytes(b'Synthetic fixture, not a real site.\n')
        self.assertEqual(git_run(['add','README.md']).returncode,0)
        result=git_run(['commit','-m','fixture: initial state'])
        self.assertEqual(result.returncode,0,result.stderr)
        regular_runner=self.site.runner
        def runner(argv,**kwargs):
            return git_run(argv[1:]) if argv[0]=='git' else regular_runner(argv,**kwargs)
        self.site.runner=runner
        dirty=self.checkout/'owner-note.txt';dirty.write_bytes(b'Owner work must survive.\n')
        with self.assertRaises(Stop):self.site.dispatch(self.request)
        self.assertEqual(dirty.read_bytes(),b'Owner work must survive.\n')
        self.assertEqual(self.calls,[])
        dirty.unlink()
        receipt=self.site.dispatch(self.request)
        self.assertEqual(receipt['status'],'accepted')
        self.assertEqual(len(receipt['source_commit']),40)
        self.assertEqual(git_run(['status','--porcelain']).stdout.strip(),'')
        result=self.site.dispatch(dict(action='rollback',operation_id=self.bundle['operation_id']))
        self.assertEqual(result['status'],'rolled_back')
        self.assertEqual(git_run(['status','--porcelain']).stdout.strip(),'')
        self.assertEqual(git_run(['rev-list','--count','HEAD']).stdout.strip(),'3')
