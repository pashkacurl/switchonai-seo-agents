"""Synthetic end-to-end demonstration. No sockets, model API, commands or live site."""
import csv
import json
from pathlib import Path
import subprocess
import tempfile
from .core import Project,initialize,read_json,write_json
from .content import import_topics,research
from .pipeline import draft,approve,Publisher
from .network import HTTP
from .sitebridge import Site


def demo():
    fact='В демонстрационном процессе заявку проверяют перед ответом.'
    article={'title':'Как обработать заявку','description':'Искусственный пример для проверки программы.',
             'markdown':'## Порядок работы\n\n'+fact+'\n\nСохраните вопрос, проверьте необходимые сведения и подготовьте понятный ответ. Это тестовый материал, его нельзя публиковать как исследование рынка.',
             'claims':[{'claim':fact,'evidence_id':'E1','quote':fact}],
             'media':[{'type':'steps','items':['Получить заявку','Проверить сведения','Подготовить ответ'],'alt':'Этапы тестового процесса'}],'changes':''}
    class FakeModel:
        def generate(self,role,payload):
            if role=='brief':return {'intent':'example','reader_task':'example','business_goal':'example','angle':'example','outline':[],'gaps':[],'stop_reasons':[]}
            if role=='author':return article
            if role=='editor':return {'verdict':'accept','issues':[],'article':article}
            raise AssertionError(role)
    with tempfile.TemporaryDirectory(prefix='seo-agents-demo-') as temporary:
        base=Path(temporary);root=base/'profile';initialize(root)
        c=read_json(root/'profile.json');c['site'].update(url='https://demo.example.test',author='Тестовая редакция')
        c['limits'].update(requests_per_day=100,articles_per_day=1)
        c['sources']['approved_urls']=['https://source.example.test/facts']
        site=base/'site';site.mkdir();sitecfg=base/'site-profile.json'
        c['publish']['site_config']=str(sitecfg);write_json(root/'profile.json',c)
        (root/'business.md').write_text('Искусственный пример: '+fact+' Мы проверяем состояние очереди, подготовку текста и подтверждение файла. Настоящего бизнеса и источников в демонстрации нет.',encoding='utf-8')
        write_json(sitecfg,{'checkout':str(site),'content_dir':'content/blog','public_dir':'public','state_dir':str(base/'receipts'),
                           'site_url':c['site']['url'],'blog_path':'/blog/','build_command':['fixture-build'],'deploy_command':['fixture-deploy'],
                           'acceptance_command':['fixture-accept'],'rollback_command':['fixture-rollback'],'timeout_seconds':30})
        row={'topic_id':'DEMO1','cluster_id':'C1','page_id':'P1','slug':'demo','title':article['title'],'primary_query':'тестовая заявка','query_ids':'Q1',
             'category':'Пример','source_urls':c['sources']['approved_urls'][0],'serp_urls':'https://competitor.example.test/demo','evidence_status':'confirmed','serp_run_id':'SYNTHETIC-ONLY'}
        path=root/'topics.csv'
        with path.open('w',encoding='utf-8',newline='') as f:
            writer=csv.DictWriter(f,fieldnames=list(row));writer.writeheader();writer.writerow(row)
        project=Project(root);commands=[]
        try:
            def fetch(url):
                text='<html><title>Пример</title><h1>Пример</h1><p>'+fact*6+'</p></html>'
                bundle_path=project.draft_dir('DEMO1')/'bundle.json'
                if url.endswith('/robots.txt'):text='User-agent: *\nAllow: /'
                elif url.endswith('/sitemap.xml'):text='<urlset><url><loc>https://demo.example.test/blog/demo/</loc></url></urlset>'
                elif url.endswith('.svg'):text='<svg/>'
                elif url.startswith('https://demo.example.test/blog/'):
                    b=read_json(bundle_path)
                    text='<html><head><title>'+b['title']+'</title><link rel="canonical" href="'+b['url']+'"><meta name="seo-revision" content="'+b['revision']+'"></head><body><h1>'+b['title']+'</h1></body></html>'
                return {'status':200,'url':url,'headers':{},'text':text}
            http=HTTP(project,True,transport=lambda url,*args:fetch(url))
            def runner(argv,**kwargs):
                commands.append(argv[0]);return subprocess.CompletedProcess(argv,0,'','')
            bridge=Site(sitecfg,runner=runner,fetch=fetch)
            import_topics(project,path);research(project,'DEMO1',http);draft(project,'DEMO1',FakeModel());approve(project,'DEMO1')
            publisher=Publisher(project,http,site=bridge);first=publisher.publish('DEMO1',True);second=publisher.publish('DEMO1',True)
            assert first['operation_id']==second['operation_id'] and commands.count('fixture-deploy')==1
            return {'mode':'OFFLINE_SYNTHETIC_DEMO','network_requests':0,'model_calls':0,'real_publications':0,
                    'stages':['import','research fixture','brief fixture','author fixture','editor fixture','SVG','approval','site receipt fixture','repeat without deploy'],
                    'status':'passed','note':'Проверена механика на искусственных данных. Авторизация, качество модели и ваш сайт требуют отдельной приёмки.'}
        finally:project.close()
