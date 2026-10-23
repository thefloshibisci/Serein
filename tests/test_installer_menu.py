import pytest
import importlib.util
import json
from pathlib import Path


def manager():
    spec=importlib.util.spec_from_file_location('installer',Path(__file__).parents[1]/'scripts'/'manage.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module


@pytest.mark.parametrize('installed',[False,True])
def test_menu_one_updates_existing_instance_and_keeps_first_install(tmp_path,monkeypatch,installed):
    module=manager();deploy=tmp_path/'deploy';deploy.mkdir();calls=[];menus=[]
    if installed:(deploy/'config.toml').write_text('synthetic')
    monkeypatch.setattr(module,'DEPLOY',deploy)
    monkeypatch.setitem(module.sys.modules,'installer',module)
    choices=iter(['1','q'])
    def choose(title,options,**kwargs):
        menus.append(dict(options));return next(choices)
    monkeypatch.setattr(module,'choose',choose)
    monkeypatch.setattr(module,'select_environment',lambda:calls.append('environment'))
    monkeypatch.setattr(module,'ensure_tools',lambda:calls.append('tools'))
    monkeypatch.setattr(module,'deploy',lambda:calls.append('install'))
    monkeypatch.setattr(module,'pause',lambda:None)
    original=module.importlib.util.spec_from_file_location
    def spec_for(name,path):
        spec=original(name,path)
        if name=='upstream_update':
            def load(helper):
                def update(actual):
                    assert actual is module
                    calls.append('update');return True
                helper.run_update=update
            spec.loader.exec_module=load
        return spec
    monkeypatch.setattr(module.importlib.util,'spec_from_file_location',spec_for)
    module.main()
    assert calls==(['update'] if installed else ['environment','tools','install'])
    assert menus[0]['1']==('拉取上游代码并重建' if installed else '安装 Serein（全新安装／旧库迁移）')
    assert menus[0]['11']=='旧 Scene 补 cues'
    assert not any('本地源码' in label for label in menus[0].values())


@pytest.mark.parametrize('confirmed',[False,True])
def test_history_repair_menu_previews_before_confirmation_and_stops_only_for_apply(tmp_path,monkeypatch,confirmed):
    module=manager();deploy=tmp_path/'deploy';deploy.mkdir();(deploy/'config.toml').write_text('synthetic')
    source=tmp_path/'old';source.mkdir()
    monkeypatch.setattr(module,'DEPLOY',deploy)
    monkeypatch.setattr(module,'ask',lambda *a:str(source))
    monkeypatch.setattr(module,'confirm',lambda *a:confirmed)
    calls=[]
    monkeypatch.setattr(module,'container_command',lambda *a,**kw:calls.append(a))
    monkeypatch.setattr(module,'service_action',lambda *a:calls.append(a))
    module.repair_legacy_history()
    assert calls==[('repair-history','/legacy/input')]+([('stop',),('repair-history','/legacy/input','--apply'),('up',)] if confirmed else [])


@pytest.mark.parametrize('confirmed',[False,True])
def test_cue_repair_menu_previews_before_confirmation_and_stops_only_for_apply(tmp_path,monkeypatch,confirmed):
    module=manager();deploy=tmp_path/'deploy';deploy.mkdir();(deploy/'config.toml').write_text('synthetic')
    monkeypatch.setattr(module,'DEPLOY',deploy)
    monkeypatch.setattr(module,'confirm',lambda *a:confirmed)
    calls=[]
    monkeypatch.setattr(module,'container_command',lambda *a,**kw:calls.append(a))
    monkeypatch.setattr(module,'service_action',lambda *a:calls.append(a))
    module.repair_legacy_cues()
    assert calls==[('repair-cues',)]+([('stop',),('repair-cues','--apply'),('up',)] if confirmed else [])


def public_access():
    spec=importlib.util.spec_from_file_location('public_access',Path(__file__).parents[1]/'scripts'/'public_access.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module


def test_https_replaces_only_unused_root_and_preserves_other_routes():
    import pytest
    module=public_access()
    text='''server {
    listen 443 ssl;
    server_name memory.example.org;
    location /legacy/ { proxy_pass http://127.0.0.1:9999; }
    location / {
        add_header X-Robots-Tag "noindex" always;
        return 410;
    }
}
server {
    listen 80;
    server_name memory.example.org;
    return 301 https://$host$request_uri;
}
'''
    changed=module.replace_placeholder(text,'memory.example.org',18217,'test-owner')
    assert 'proxy_pass http://127.0.0.1:18217;' in changed
    assert 'location /legacy/ { proxy_pass http://127.0.0.1:9999; }' in changed
    assert changed.split('server {')[2]==text.split('server {')[2]
    assert 'Host $http_host;' in changed and 'proxy_buffering off;' in changed
    assert module.replace_placeholder(changed,'memory.example.org',18217,'test-owner')==changed
    with pytest.raises(ValueError,match='已有内容'):
        module.replace_placeholder(text.replace('return 410;','try_files $uri /index.html;'),'memory.example.org',18217,'test-owner')
    with pytest.raises(ValueError):module.host('example.org; include /tmp/file;')


def test_nginx_validation_failure_restores_exact_configuration(tmp_path):
    import subprocess
    import pytest
    module=public_access();target=tmp_path/'site.conf';target.write_text('original config')
    calls=[]
    def run(args,**kw):
        calls.append(args)
        if len(calls)==1:raise subprocess.CalledProcessError(1,args)
    with pytest.raises(ValueError,match='已恢复'):
        module.apply(target,'invalid config',tmp_path/'deploy',runner=run)
    assert target.read_text()=='original config'
    assert calls==[['nginx','-t'],['nginx','-t'],['systemctl','reload','nginx']]
    assert len(list((tmp_path/'deploy/runtime/nginx-backups').glob('*.conf')))==1


def test_public_ip_access_updates_only_gateway_and_preserves_credentials(tmp_path,monkeypatch):
    module=manager();deploy=tmp_path/'deploy';(deploy/'secrets').mkdir(parents=True)
    for name in ('web-auth.json','api-token'):(deploy/'secrets'/name).write_text('existing secret')
    (deploy/'.env').write_text('SEREIN_BIND=127.0.0.1\nSEREIN_PORT=19217\n')
    (deploy/'installation.json').write_text('{"backend":"docker","compose_project":"test-instance","client_host":"127.0.0.1"}')
    monkeypatch.setattr(module,'DEPLOY',deploy);answers=iter(['1','203.0.113.10'])
    monkeypatch.setattr(module,'ask',lambda *args:next(answers));monkeypatch.setattr(module,'confirm',lambda *args:True)
    calls=[];monkeypatch.setattr(module,'compose',lambda *args:calls.append(args))
    module.access()
    assert module.url()=='http://203.0.113.10:19217'
    assert module.read_env()['SEREIN_BIND']=='0.0.0.0'
    assert calls==[('up','-d','--no-deps','--wait','gateway')]
    assert all(p.read_text()=='existing secret' for p in (deploy/'secrets').iterdir())
    assert module.installation()['compose_project']=='test-instance'
    assert 'X-Serein-Window-ID' in (deploy/'connection-guide.txt').read_text('utf-8')


def test_public_access_failed_start_restores_bind_and_client(tmp_path,monkeypatch):
    import subprocess
    import pytest
    module=manager();deploy=tmp_path/'deploy';(deploy/'secrets').mkdir(parents=True)
    for name in ('web-auth.json','api-token'):(deploy/'secrets'/name).write_text('existing secret')
    (deploy/'.env').write_text('SEREIN_BIND=127.0.0.1\nSEREIN_PORT=19217\n')
    before='{"backend":"docker","client_host":"127.0.0.1"}'
    (deploy/'installation.json').write_text(before)
    monkeypatch.setattr(module,'DEPLOY',deploy);answers=iter(['1','203.0.113.10'])
    monkeypatch.setattr(module,'ask',lambda *args:next(answers));monkeypatch.setattr(module,'confirm',lambda *args:True)
    calls=[]
    def compose(*args):
        calls.append(args)
        if len(calls)==1:raise subprocess.CalledProcessError(1,['docker'])
    monkeypatch.setattr(module,'compose',compose)
    with pytest.raises(subprocess.CalledProcessError):module.access()
    assert module.read_env()['SEREIN_BIND']=='127.0.0.1'
    assert (deploy/'installation.json').read_text()==before
    assert len(calls)==2


@pytest.mark.parametrize('generate_cues',[False,True])
def test_migration_keeps_new_writers_stopped_until_wizard_returns(tmp_path,monkeypatch,generate_cues):
    module=manager();deploy=tmp_path/'new'/'deploy';(deploy/'runtime').mkdir(parents=True)
    old=tmp_path/'old';old.mkdir();answers=iter(['0',str(old),'Mira','Sol','米拉','1' if generate_cues else '0'])
    actions=[]
    monkeypatch.setattr(module,'DEPLOY',deploy)
    monkeypatch.setattr(module,'ask',lambda *a:next(answers))
    monkeypatch.setattr(module,'confirm',lambda *a:True)
    monkeypatch.setattr(module,'prepare',lambda:None)
    (deploy/'secrets').mkdir(parents=True)
    (deploy/'secrets/api-token').write_text('synthetic-key')
    monkeypatch.setattr(module,'configure_client',lambda:None)
    monkeypatch.setattr(module,'url',lambda:'http://localhost:1')
    monkeypatch.setattr(module,'compose',lambda *a,**k:actions.append(a))
    monkeypatch.setattr(module,'container_command',lambda *a,**k:actions.append(a))
    monkeypatch.setattr(module,'stop_old',lambda *a:actions.append(('stop_old',)))
    monkeypatch.setattr(module,'check_memory',lambda:None)
    module.deploy()
    assert actions[:3]==[('stop','gateway','memory'),('build','memory'),('build','gateway')]
    assert [a[0] for a in actions]==['stop','build','build','scan','stop_old','stop','create','wizard','up']
    options=json.loads((deploy/'runtime'/'migration-options.json').read_text('utf-8'))
    assert options['user_name']=='Mira' and options['ai_name']=='Sol' and options['aliases']==['米拉']
    assert options['generate_cues'] is generate_cues


def test_docker_web_migration_authorizes_one_read_only_source(tmp_path,monkeypatch):
    module=manager();deploy=tmp_path/'new'/'deploy';deploy.mkdir(parents=True)
    (deploy/'config.toml').write_text('synthetic config')
    (deploy/'installation.json').write_text('{"backend":"docker","compose_project":"test-instance"}')
    old=tmp_path/'old-ombre';old.mkdir()
    monkeypatch.setattr(module,'DEPLOY',deploy)
    monkeypatch.setattr(module,'ask',lambda *args:str(old))
    monkeypatch.setattr(module,'confirm',lambda *args:True)
    calls=[];monkeypatch.setattr(module,'compose',lambda *args:calls.append(args))
    module.legacy_source_access()
    assert module.installation()['legacy_source_root']==str(old)
    assert calls==[('up','-d','--no-deps','--force-recreate','--wait','memory'),
                   ('up','-d','--no-deps','--force-recreate','--wait','gateway')]


def test_docker_build_does_not_continue_after_stop_or_build_failure(monkeypatch):
    import subprocess
    import pytest
    module=manager()
    monkeypatch.setattr(module,'backend',lambda:'docker')
    for failed_step in ('stop','build'):
        calls=[]
        def compose(*args):
            calls.append(args)
            if args[0]==failed_step:raise subprocess.CalledProcessError(1,['docker'])
        monkeypatch.setattr(module,'compose',compose)
        with pytest.raises(subprocess.CalledProcessError):module.build_runtime()
        assert calls[0]==('stop','gateway','memory')
        assert ('build','gateway') not in calls
        assert not any(call[0]=='up' for call in calls)


@pytest.mark.parametrize('selection,expected',[('',True),('0',False),('r',None)])
def test_update_backup_choice_defaults_to_backup_and_can_cancel(tmp_path,monkeypatch,selection,expected):
    module=manager();deploy=tmp_path/'deploy';(deploy/'runtime').mkdir(parents=True)
    (deploy/'runtime'/'serein.db').write_bytes(b'synthetic')
    (deploy/'secrets').mkdir();(deploy/'secrets'/'api-token').write_text('synthetic')
    answers=iter(['1',selection]);calls=[]
    monkeypatch.setattr('builtins.input',lambda *a:next(answers))
    monkeypatch.setattr(module,'DEPLOY',deploy)
    monkeypatch.setattr(module,'prepare',lambda:calls.append('prepare'))
    monkeypatch.setattr(module,'configure_client',lambda:None)
    monkeypatch.setattr(module,'build_runtime',lambda **options:calls.append(options))
    monkeypatch.setattr(module,'service_action',lambda *a:calls.append(a))
    monkeypatch.setattr(module,'url',lambda:'http://localhost:1')
    monkeypatch.setattr(module,'check_memory',lambda:None)
    module.deploy()
    assert calls==([] if expected is None else ['prepare',{'backup':expected},('up',)])


@pytest.mark.parametrize('kind',['docker','local'])
@pytest.mark.parametrize('backup',[False,True])
def test_optional_backup_keeps_stop_before_build_order(tmp_path,monkeypatch,kind,backup):
    module=manager();calls=[]
    python=tmp_path/'python';python.touch()
    npm=tmp_path/'node_modules/npm/bin/npm-cli.js';npm.parent.mkdir(parents=True);npm.touch()
    monkeypatch.setattr(module,'backend',lambda:kind)
    monkeypatch.setattr(module,'local_python',lambda:python)
    monkeypatch.setattr(module.shutil,'which',lambda name:str(tmp_path/name))
    monkeypatch.setattr(module,'compose',lambda *args:calls.append(args))
    monkeypatch.setattr(module,'local_action',lambda *args:calls.append(args))
    monkeypatch.setattr(module,'backup_runtime',lambda:calls.append(('backup',)))
    monkeypatch.setattr(module,'run',lambda *args,**kwargs:calls.append(('run',)))
    module.build_runtime(backup=backup)
    assert calls[0]==(('stop','gateway','memory') if kind=='docker' else ('stop',))
    assert calls.count(('backup',))==int(backup)
    if backup:assert calls[1]==('backup',)
    assert calls[-1]==(('build','gateway') if kind=='docker' else ('run',))


def test_upgrade_backup_contains_data_and_credentials_without_recursing_backups(tmp_path,monkeypatch):
    import tarfile
    module=manager();deploy=tmp_path/'deploy'
    (deploy/'runtime').mkdir(parents=True);(deploy/'runtime'/'serein.db').write_bytes(b'synthetic-db')
    (deploy/'secrets').mkdir();(deploy/'secrets'/'api-token').write_text('synthetic-secret')
    (deploy/'config.toml').write_text('synthetic config')
    monkeypatch.setattr(module,'DEPLOY',deploy)
    module.backup_runtime();module.backup_runtime()
    backups=list((deploy/'backups').glob('*.tar.gz'));assert len(backups)==2
    for backup in backups:
        with tarfile.open(backup) as archive:
            assert archive.extractfile('runtime/serein.db').read()==b'synthetic-db'
            assert archive.extractfile('secrets/api-token').read()==b'synthetic-secret'
            assert not any('backups' in name for name in archive.getnames())


def test_docker_restart_waits_for_gateway_health(monkeypatch):
    module=manager();calls=[]
    monkeypatch.setattr(module,'backend',lambda:'docker')
    monkeypatch.setattr(module,'compose',lambda *args:calls.append(args))
    module.service_action('restart','gateway')
    assert calls==[('restart','gateway'),('up','-d','--wait','gateway')]


def test_auth_stores_only_salted_hash_and_prepare_preserves_existing_data(tmp_path,monkeypatch,capsys):
    module=manager();deploy=tmp_path/'deploy';monkeypatch.setattr(module,'DEPLOY',deploy)
    monkeypatch.setattr(module,'ask',lambda text,default='':default)
    monkeypatch.setattr(module.getpass,'getpass',lambda *a:'a-long-test-password')
    module.prepare()
    record=json.loads((deploy/'secrets'/'web-auth.json').read_text())
    assert record['username']=='admin' and len(record['hash'])==64
    before=(deploy/'secrets'/'api-token').read_bytes()
    (deploy/'runtime'/'sentinel').write_text('preserve')
    module.prepare()
    assert (deploy/'runtime'/'sentinel').read_text()=='preserve'
    assert (deploy/'secrets'/'api-token').read_bytes()==before
    assert 'a-long-test-password' not in capsys.readouterr().out
    assert '/data/serein.db' in (deploy/'config.toml').read_text(encoding='utf-8')


def test_invalid_choice_and_confirmation_retry_without_running_actions(monkeypatch):
    module=manager();answers=iter(['wrong','r','maybe',''])
    monkeypatch.setattr(module,'ask',lambda *a:next(answers))
    assert module.choose('部署',[('0','旧库'),('1','新部署')],back=True)=='r'
    assert module.confirm('继续') is False


def test_account_menu_does_not_require_docker_and_eof_exits(tmp_path,monkeypatch):
    module=manager();actions=[];answers=iter(['0'])
    monkeypatch.setattr(module,'DEPLOY',tmp_path/'deploy')
    monkeypatch.setattr(module.sys,'platform','linux')
    def ask(*args):
        try:return next(answers)
        except StopIteration:raise EOFError
    monkeypatch.setattr(module,'ask',ask)
    monkeypatch.setattr(module,'pause',lambda:None)
    monkeypatch.setattr(module,'auth',lambda:actions.append('auth'))
    monkeypatch.setattr(module,'ensure_tools',lambda:actions.append('docker'))
    module.main()
    assert actions==['auth']


def test_local_prepare_uses_own_paths_preserves_secrets_and_retries_duplicate_ports(tmp_path,monkeypatch):
    module=manager();deploy=tmp_path/'deploy';deploy.mkdir()
    (deploy/'installation.json').write_text('{"backend":"local"}')
    monkeypatch.setattr(module,'DEPLOY',deploy)
    answers=iter(['admin','127.0.0.1','19200','19200','19201','19201','19202'])
    monkeypatch.setattr(module,'ask',lambda *a:next(answers))
    monkeypatch.setattr(module.getpass,'getpass',lambda *a:'a-long-test-password')
    module.prepare();module.configure_client()
    before=(deploy/'secrets'/'api-token').read_bytes()
    (deploy/'runtime'/'sentinel').write_text('preserve')
    module.prepare()
    assert (deploy/'secrets'/'api-token').read_bytes()==before
    assert (deploy/'runtime'/'sentinel').read_text()=='preserve'
    assert '"./runtime/serein.db"' in (deploy/'config.toml').read_text(encoding='utf-8')
    assert module.url()=='http://127.0.0.1:19200'
    record=module.installation()
    assert (record['memory_port'],record['preview_port'])==(19201,19202)


def test_local_migration_failure_does_not_start_services(tmp_path,monkeypatch):
    import subprocess
    import pytest
    module=manager();deploy=tmp_path/'deploy';(deploy/'runtime').mkdir(parents=True)
    (deploy/'installation.json').write_text('{"backend":"local"}')
    old=tmp_path/'old.tar.gz';old.write_bytes(b'fixture')
    answers=iter(['0',str(old),'Mira','Sol','','1'])
    actions=[]
    monkeypatch.setattr(module,'DEPLOY',deploy)
    monkeypatch.setattr(module,'ask',lambda *a:next(answers))
    monkeypatch.setattr(module,'confirm',lambda *a:True)
    monkeypatch.setattr(module,'prepare',lambda:None)
    monkeypatch.setattr(module,'configure_client',lambda:None)
    monkeypatch.setattr(module,'build_runtime',lambda **kwargs:actions.append('build'))
    monkeypatch.setattr(module,'local_action',lambda *a:actions.append(a))
    def migration(*args,**kwargs):
        actions.append(args[0])
        if args[0]=='wizard':raise subprocess.CalledProcessError(1,['fixture'])
    monkeypatch.setattr(module,'container_command',migration)
    with pytest.raises(subprocess.CalledProcessError):module.deploy()
    assert actions==['build','scan',('stop','all'),'wizard']


def test_local_migration_passes_host_paths_without_shell(tmp_path,monkeypatch):
    module=manager();deploy=tmp_path/'含空格 new'/'deploy';deploy.mkdir(parents=True)
    (deploy/'installation.json').write_text('{"backend":"local"}')
    monkeypatch.setattr(module,'DEPLOY',deploy)
    calls=[];monkeypatch.setattr(module,'run',lambda *a,**kw:calls.append((a,kw)))
    source=tmp_path/'旧库 with spaces.tar.gz'
    module.container_command('wizard','/legacy/input','--options','/data/migration-options.json',source=source)
    command=calls[0][0][0]
    assert str(source) in command and str(deploy/'runtime'/'migration-options.json') in command
    assert '/legacy/input' not in command and not calls[0][1].get('shell')


def test_environment_back_does_not_write_and_windows_menu_can_exit(tmp_path,monkeypatch):
    module=manager();monkeypatch.setattr(module,'DEPLOY',tmp_path/'deploy')
    monkeypatch.setattr(module,'ask',lambda *a:'r')
    assert module.select_environment() is False
    assert not (tmp_path/'deploy').exists()
    monkeypatch.setattr(module.sys,'platform','win32')
    monkeypatch.setattr(module,'ask',lambda *a:'q')
    module.main()


def test_fresh_docker_directories_have_different_projects_and_old_config_stays_compatible(tmp_path,monkeypatch):
    module=manager();monkeypatch.setattr(module,'ask',lambda *a:'1')
    projects=[]
    for name in ('first','second'):
        monkeypatch.setattr(module,'DEPLOY',tmp_path/name/'deploy')
        module.select_environment();projects.append(module.installation()['compose_project'])
    assert projects[0]!=projects[1]
    old=tmp_path/'old';old.mkdir();(old/'config.toml').write_text('existing')
    monkeypatch.setattr(module,'DEPLOY',old)
    module.select_environment()
    calls=[];monkeypatch.setattr(module,'run',lambda args,**kw:calls.append(args))
    module.compose('ps')
    assert calls[0][3]=='serein-public'


def test_rotate_gateway_key_updates_guide_and_restarts_both_services(tmp_path,monkeypatch,capsys):
    module=manager();deploy=tmp_path/'deploy';(deploy/'secrets').mkdir(parents=True)
    (deploy/'secrets/api-token').write_text('old-key');(deploy/'secrets/web-auth.json').write_text('page-auth')
    (deploy/'.env').write_text('SEREIN_PORT=19217\n')
    monkeypatch.setattr(module,'DEPLOY',deploy)
    monkeypatch.setattr(module,'confirm',lambda *args:True)
    calls=[];monkeypatch.setattr(module,'service_action',lambda *args:calls.append(args))
    module.rotate_key()
    key=(deploy/'secrets/api-token').read_text();assert len(key)>=40 and key!='old-key'
    assert key in (deploy/'connection-guide.txt').read_text('utf-8') and key in capsys.readouterr().out
    assert 'http://127.0.0.1:19217/serein/mcp' in (deploy/'connection-guide.txt').read_text('utf-8')
    assert calls==[('restart',)] and (deploy/'secrets/web-auth.json').read_text()=='page-auth'
    monkeypatch.setattr(module,'confirm',lambda *args:False);module.rotate_key()
    assert (deploy/'secrets/api-token').read_text()==key and len(calls)==1


@pytest.mark.parametrize('public_url', ['', 'https://memory.example.test'])
def test_menu_rotate_key_without_env_still_shows_key(tmp_path,monkeypatch,capsys,public_url):
    module=manager();deploy=tmp_path/'deploy';(deploy/'secrets').mkdir(parents=True)
    (deploy/'secrets/api-token').write_text('old-key')
    (deploy/'secrets/web-auth.json').write_text('page-auth')
    record={'backend':'docker'}
    if public_url:record['public_url']=public_url
    (deploy/'installation.json').write_text(json.dumps(record))
    monkeypatch.setattr(module,'DEPLOY',deploy)
    choices=iter(['6','q']);monkeypatch.setattr(module,'choose',lambda *a,**kw:next(choices))
    monkeypatch.setattr(module,'confirm',lambda *a:True)
    monkeypatch.setattr(module,'ensure_tools',lambda:None)
    monkeypatch.setattr(module,'pause',lambda:None)
    calls=[]
    monkeypatch.setattr(module,'service_action',lambda *a:calls.append((a,(deploy/'secrets/api-token').read_text())))
    module.main()
    key=(deploy/'secrets/api-token').read_text();output=capsys.readouterr().out
    assert key!='old-key' and calls==[(('restart',),key)]
    assert f'Gateway Key：{key}' in output and '未完成：' not in output
    guide=(deploy/'connection-guide.txt').read_text('utf-8')
    assert key in guide and not (deploy/'.env').exists()
    assert (deploy/'secrets/web-auth.json').read_text()=='page-auth'
    assert json.loads((deploy/'installation.json').read_text())==record
    if public_url:
        assert public_url+'/serein/mcp' in guide and '访问地址未读取' not in guide
    else:
        assert '访问地址未读取' in guide and '沿用原来的' in guide
        assert 'http://' not in guide and 'https://' not in guide


def test_rotate_key_keeps_success_and_prints_key_when_guide_cannot_be_saved(tmp_path,monkeypatch,capsys):
    module=manager();deploy=tmp_path/'deploy';(deploy/'secrets').mkdir(parents=True)
    (deploy/'secrets/api-token').write_text('old-key')
    (deploy/'.env').write_text('SEREIN_PORT=19217\n')
    monkeypatch.setattr(module,'DEPLOY',deploy);monkeypatch.setattr(module,'confirm',lambda *a:True)
    calls=[];monkeypatch.setattr(module,'service_action',lambda *a:calls.append(a))
    write=module.private_file
    def private_file(path,text):
        if path.name=='connection-guide.txt':raise PermissionError('synthetic denied')
        return write(path,text)
    monkeypatch.setattr(module,'private_file',private_file)
    module.rotate_key()
    key=(deploy/'secrets/api-token').read_text();output=capsys.readouterr().out
    assert key!='old-key' and f'Gateway Key：{key}' in output
    assert '连接说明文件未能保存' in output and calls==[('restart',)]


def test_rotate_gateway_key_failure_restores_old_key(tmp_path,monkeypatch):
    import pytest
    module=manager();deploy=tmp_path/'deploy';(deploy/'secrets').mkdir(parents=True)
    (deploy/'secrets/api-token').write_text('old-key');(deploy/'connection-guide.txt').write_text('old-guide')
    monkeypatch.setattr(module,'DEPLOY',deploy);monkeypatch.setattr(module,'confirm',lambda *args:True)
    calls=[]
    def action(*args):
        calls.append(args)
        if len(calls)==1:raise RuntimeError('restart failed')
    monkeypatch.setattr(module,'service_action',action)
    with pytest.raises(RuntimeError):module.rotate_key()
    assert (deploy/'secrets/api-token').read_text()=='old-key'
    assert (deploy/'connection-guide.txt').read_text()=='old-guide' and len(calls)==2


@pytest.mark.parametrize('source_given',[True,False])
def test_edge_repair_menu_previews_stops_and_restarts_without_full_migration(tmp_path,monkeypatch,source_given):
    module=manager();deploy=tmp_path/'deploy';deploy.mkdir();(deploy/'config.toml').write_text('synthetic')
    source=tmp_path/'backup';source.mkdir();calls=[]
    monkeypatch.setattr(module,'DEPLOY',deploy)
    monkeypatch.setattr(module,'ask',lambda *a:str(source) if source_given else '')
    monkeypatch.setattr(module,'confirm',lambda *a:True)
    monkeypatch.setattr(module,'container_command',lambda *a,**kw:calls.append((a,kw)))
    monkeypatch.setattr(module,'service_action',lambda *a:calls.append(a))
    module.repair_legacy_edges()
    args=('repair-edges','/legacy/input') if source_given else ('repair-edges',)
    assert calls==[(args,{'source':source if source_given else None}),('stop',),
                   ((*args,'--apply'),{'source':source if source_given else None}),('up',)]


def test_edge_repair_menu_cancel_does_not_stop_or_write(tmp_path,monkeypatch):
    module=manager();(tmp_path/'config.toml').write_text('synthetic')
    monkeypatch.setattr(module,'DEPLOY',tmp_path)
    monkeypatch.setattr(module,'ask',lambda *a:'')
    monkeypatch.setattr(module,'confirm',lambda *a:False)
    calls=[];monkeypatch.setattr(module,'container_command',lambda *a,**kw:calls.append(a))
    def forbidden(*a):raise AssertionError('must not stop services')
    monkeypatch.setattr(module,'service_action',forbidden)
    module.repair_legacy_edges()
    assert calls==[('repair-edges',)]



def backup_helper():
    spec=importlib.util.spec_from_file_location('runtime_backup_test',Path(__file__).parents[1]/'scripts/runtime_backup.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module


def synthetic_backups(deploy):
    import os
    folder=deploy/'backups';folder.mkdir(parents=True)
    files=[]
    for i in range(1,6):
        path=folder/f'20260913T12000{i}Z-{i:08x}.tar.gz';path.write_bytes(b'x'*i)
        os.utime(path,ns=(i*1000000000,i*1000000000));files.append(path)
    return files


def test_backup_cleanup_keeps_latest_three_and_does_not_touch_other_data(tmp_path):
    helper=backup_helper();files=synthetic_backups(tmp_path)
    for relative in ['backups/manual.tar.gz','backups/20260913T120006Z-00000006.tar.pending','runtime/serein.db','secrets/api-token']:
        path=tmp_path/relative;path.parent.mkdir(exist_ok=True);path.write_bytes(b'keep')
    plan=helper.cleanup_plan(tmp_path)
    assert [row['name'] for row in plan['remove']]==[files[1].name,files[0].name]
    assert helper.cleanup(tmp_path,plan)=={'removed':2,'bytes':3,'retained':3}
    assert [p.exists() for p in files]==[False,False,True,True,True]
    assert all((tmp_path/r).read_bytes()==b'keep' for r in ['backups/manual.tar.gz','backups/20260913T120006Z-00000006.tar.pending','runtime/serein.db','secrets/api-token'])
    assert helper.cleanup_plan(tmp_path)['remove']==[]
    with pytest.raises(ValueError,match='至少保留'):helper.cleanup_plan(tmp_path,0)


def test_backup_cleanup_rejects_changed_preview_before_any_delete(tmp_path):
    helper=backup_helper();files=synthetic_backups(tmp_path);plan=helper.cleanup_plan(tmp_path)
    files[0].write_bytes(b'changed')
    with pytest.raises(ValueError,match='清单已变化'):helper.cleanup(tmp_path,plan)
    assert all(p.exists() for p in files)
    plan=helper.cleanup_plan(tmp_path);plan['remove']=[{'name':'../runtime/serein.db'}]
    with pytest.raises(ValueError):helper.cleanup(tmp_path,plan)
    assert all(p.exists() for p in files)


def test_backup_cleanup_rejects_linked_directory(tmp_path):
    helper=backup_helper();outside=tmp_path/'outside';outside.mkdir();deploy=tmp_path/'deploy';deploy.mkdir()
    try:(deploy/'backups').symlink_to(outside,target_is_directory=True)
    except OSError:pytest.skip('symlinks unavailable')
    with pytest.raises(ValueError,match='符号链接'):helper.cleanup_plan(deploy)


def test_backup_menu_cancel_preserves_all_files_and_needs_no_services(tmp_path,monkeypatch):
    module=manager();files=synthetic_backups(tmp_path)
    monkeypatch.setattr(module,'DEPLOY',tmp_path)
    monkeypatch.setattr(module,'ask',lambda *a:'3')
    monkeypatch.setattr(module,'confirm',lambda *a:False)
    def forbidden(*a):raise AssertionError('cleanup must work without runtime dependencies or restarting services')
    monkeypatch.setattr(module,'ensure_tools',forbidden)
    monkeypatch.setattr(module,'service_action',forbidden)
    choices=iter(['9','q']);monkeypatch.setattr(module,'choose',lambda *a,**kw:next(choices));monkeypatch.setattr(module,'pause',lambda:None)
    module.main()
    assert all(p.exists() for p in files)


def test_backup_menu_confirms_preview_then_deletes_only_old_files(tmp_path,monkeypatch,capsys):
    module=manager();files=synthetic_backups(tmp_path)
    monkeypatch.setattr(module,'DEPLOY',tmp_path)
    answers=iter(['0','3']);monkeypatch.setattr(module,'ask',lambda *a:next(answers))
    def confirm(*a):
        output=capsys.readouterr().out
        assert files[0].name in output and files[1].name in output and '可释放' in output
        assert all(p.exists() for p in files)
        return True
    monkeypatch.setattr(module,'confirm',confirm)
    module.cleanup_backups()
    assert [p.exists() for p in files]==[False,False,True,True,True]


@pytest.mark.parametrize('backend', ['docker','local'])
@pytest.mark.parametrize('ready', [True,False])
def test_runtime_policy_check_reports_state_without_printing_process_output(tmp_path,monkeypatch,capsys,backend,ready):
    from types import SimpleNamespace
    module=manager();monkeypatch.setattr(module,'DEPLOY',tmp_path)
    monkeypatch.setattr(module,'backend',lambda:backend)
    state=({'ready':True,'route_source':'prepared','routes':5,'boundaries':11,'domain_source':'instance'} if ready
           else {'ready':False,'stage':'live_policy','reason':'route_source_missing'})
    calls=[]
    def process(*args,**kwargs):
        calls.append((args,kwargs));return SimpleNamespace(stdout=json.dumps(state),stderr='secret must not print')
    monkeypatch.setattr(module,'compose',process);monkeypatch.setattr(module,'run',process)
    assert module.check_memory()==state
    output=capsys.readouterr().out
    assert ('校验通过' if ready else 'route_source_missing') in output
    assert 'secret must not print' not in output
    assert calls[0][1]['capture_output'] and calls[0][1]['timeout']==30
