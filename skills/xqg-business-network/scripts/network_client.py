# Connection storage keeps the pre-rename path to preserve existing identities.
"""网络客户端：查询、告知后的内部记录；不带数据库或共享凭证，不自动登记公开档案。"""
import argparse,json,os,re,sqlite3,sys,secrets,hashlib,stat
from pathlib import Path
from urllib import request,error,parse

ROOT=Path(__file__).resolve().parents[1]
def scrub(value):
    text=str(value or '')
    text=re.sub(r'(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)','[联系方式不展示]',text)
    text=re.sub(r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}','[联系方式不展示]',text)
    return re.sub(r'(?:微信号?|wechat)\s*[:：]\s*[A-Za-z][A-Za-z0-9_-]{5,}','[联系方式不展示]',text,flags=re.I)

def project_person(p):
    """只接受展示字段；不转发服务端未知字段、联系方式、原始文件路径。"""
    return dict(person_id=str(p.get('person_id','')),name=scrub(p.get('name')),
        cities=[scrub(v) for v in p.get('cities',[])],businesses=[scrub(v) for v in p.get('businesses',[])],
        companies=[scrub(v) for v in p.get('companies',[])],
        items=[dict(item_id=str(i.get('item_id','')),kind=str(i.get('kind','')),text=scrub(i.get('text')),
            status=scrub(i.get('status'))) for i in p.get('items',[]) if i.get('kind') in ('resource','need','request')],
        last_confirmed_at=p.get('last_confirmed_at'),confirmation_status=scrub(p.get('confirmation_status')),
        identity_review_required=bool(p.get('identity_review_required')))

def local(config,args):
    if args.command in ('submit','delete-submission','stop-recording'):return dict(status='not_connected',message='提交需要连接正式网络；本地查人模式不上传记录。')
    if config.get('audience')!='operator':
        return dict(status='configuration_error',message='本地历史库仅限已配置的运营者环境，不能作为公开查询源。')
    path=Path(config['database_path']).expanduser().resolve()
    if not path.is_file():return dict(status='not_connected',message='本地数据库不可用。')
    db=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True);db.row_factory=sqlite3.Row
    try:
        timestamp=db.execute("SELECT value FROM metadata WHERE key='imported_at'").fetchone()
        as_of=json.loads(timestamp[0]) if timestamp else None
        base=dict(status='ok',audience='operator',as_of=as_of,
            notice='内部历史资料；未自动同步，未确认独立人数、需求有效性或公开展示授权。')
        if args.command=='status':return dict(base,mode='local',search_available=True,scheduler_available='由宿主另行核实')
        if args.command=='stats':
            return dict(base,profile_count=db.execute("SELECT COUNT(*) FROM persons WHERE status='active'").fetchone()[0],
                raw_record_count=db.execute('SELECT COUNT(*) FROM source_records').fetchone()[0],
                public_profile_count=db.execute('SELECT COUNT(*) FROM public_directory').fetchone()[0])
        conditions="status='active'";params=[]
        if args.command=='person':
            pid=args.id.upper().strip()
            if not re.fullmatch(r'A\d+',pid):return dict(status='invalid_request',message='需要真实人员编号，如 A000002。')
            pid='A'+pid[1:].zfill(6)
            alias=db.execute('SELECT person_id FROM person_alias_ids WHERE alias_id=?',(pid,)).fetchone()
            if alias:pid=alias[0]
            conditions+=' AND person_id=?';params.append(pid)
        results=[];matched=0
        for row in db.execute('SELECT * FROM persons WHERE '+conditions+' ORDER BY sequence',params):
            p=dict(row)
            for k in ['cities','businesses','companies','roles','industries']:
                p[k]=json.loads(p[k+'_json'])
            p['items']=[dict(i) for i in db.execute('SELECT item_id,kind,text,status FROM items WHERE person_id=?',(p['person_id'],))]
            p['identity_review_required']=bool(db.execute("SELECT 1 FROM identity_review WHERE (person_a=? OR person_b=?) AND status='待人工核对' LIMIT 1",(p['person_id'],p['person_id'])).fetchone())
            if args.command=='search':
                if args.city.casefold() not in ' '.join(p['cities']).casefold():continue
                if args.kind:
                    p['items']=[i for i in p['items'] if i['kind']==args.kind]
                    if not p['items']:continue
                    searchable=' '.join(i['text'] for i in p['items'])
                else:searchable=' '.join([p['name']]+p['cities']+p['companies']+p['businesses']+p['roles']+p['industries']+[i['text'] for i in p['items']])
                if not all(q.casefold() in searchable.casefold() for q in args.query.split()):continue
            matched+=1
            if len(results)<args.limit:results.append(project_person(p))
        return dict(base,matched_profile_count=matched,results=results)
    finally:db.close()

def ssh_operator(config,args):
    import subprocess,shlex
    if config.get('audience')!='operator' or args.command not in ('status','stats','search','person'):
        return dict(status='configuration_error',message='仅运营者可通过SSH执行只读查询。')
    host=config['ssh_destination']
    if not re.fullmatch(r'[A-Za-z0-9_.@-]+',host) or host.startswith('-'):raise ValueError('invalid host')
    argv=['/usr/bin/python3',config['remote_client'],'--config',config['remote_config'],args.command]
    if args.command=='person':argv+=['--id',args.id]
    if args.command=='search':
        argv+=['--query',args.query,'--city',args.city,'--limit',str(args.limit)]
        if args.kind:argv+=['--kind',args.kind]
    try:
        result=subprocess.run(['ssh','-o','BatchMode=yes','-o','ConnectTimeout=8','-i',str(Path(config['identity_file']).expanduser()),host,shlex.join(argv)],capture_output=True,text=True,timeout=20,check=True)
        return json.loads(result.stdout)
    except (subprocess.SubprocessError,ValueError):
        return dict(status='not_connected',message='无法连接Mac mini主库；没有回退到MacBook旧快照。')

class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None

def automatic_token(base_url):
    # Persist before enrollment: retry or offline failure reuses the same identity.
    folder=Path.home()/'.config/xqg-entrepreneur-network';folder.mkdir(parents=True,exist_ok=True,mode=0o700)
    dest=folder/('session-'+hashlib.sha256(base_url.encode()).hexdigest()[:20]+'.key')
    try:
        fd=os.open(dest,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'w') as f:f.write(secrets.token_urlsafe(32))
    except FileExistsError:pass
    if dest.is_symlink():raise ValueError('unsafe session file')
    fd=os.open(dest,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0)|getattr(os,'O_NONBLOCK',0))
    with os.fdopen(fd) as f:
        info=os.fstat(f.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:raise ValueError('unsafe session file')
        token=f.read(129).strip()
    if not re.fullmatch(r'[A-Za-z0-9_-]{43}',token):raise ValueError('invalid session file')
    req=request.Request(base_url+'/v1/session',data=b'{}',headers={'Content-Type':'application/json','User-Agent':'XQG-Business-Network/0.3.5','Authorization':'Bearer '+token},method='POST')
    with request.build_opener(NoRedirect).open(req,timeout=15) as response:
        data=json.loads(response.read(4096))
    if data.get('session_ready') is not True:raise ValueError('session unavailable')
    return token

def remote(config,args):
    base_url=config.get('base_url','').rstrip('/')
    u=parse.urlsplit(base_url)
    if u.scheme!='https' or not u.hostname or u.username or u.password or u.query or u.fragment:
        return dict(status='configuration_error',message='共享连接需要运营者提供的 HTTPS 服务地址。')
    endpoint={'status':'capabilities','stats':'stats','search':'search','person':'person','submit':'submissions','delete-submission':'submissions/delete','stop-recording':'recording/stop'}[args.command]
    payload={}
    if args.command=='search':payload=dict(query=args.query,city=args.city,kind=args.kind,limit=args.limit)
    elif args.command=='person':payload=dict(person_id=args.id)
    elif args.command=='submit':
        payload=dict(id=args.id,scope=args.scope,text=Path(args.file).read_text(),notice_shown=args.notice_shown,notice_version='2026-09-14-v3' if args.scope=='conversation_turn' else '2026-09-13-v2')
    elif args.command=='delete-submission':payload=dict(id=args.id)
    headers={'Content-Type':'application/json','Accept':'application/json','User-Agent':'XQG-Business-Network/0.3.5'}
    token_var=config.get('token_env')
    token_path=config.get('token_file')
    automatic=config.get('automatic_session',False)
    if type(automatic) is not bool or (automatic and (token_var or token_path)):
        raise ValueError('ambiguous credential source')
    if automatic:
        headers['Authorization']='Bearer '+automatic_token(base_url)
    elif not (token_var or token_path):
        return dict(status='not_connected',message='连接配置不完整，请更新官方Skill；不需要人工激活。')
    if token_var or token_path:
        if token_var and token_path:raise ValueError('ambiguous credential source')
        if token_path:
            secret_file=Path(token_path).expanduser()
            if os.name=='posix' and secret_file.stat().st_mode & 0o077:
                return dict(status='configuration_error',message='凭证文件权限过宽，请设置为仅本人可读写。')
            token=secret_file.read_text().strip()
        else:token=os.environ.get(token_var)
        if not token:return dict(status='not_connected',message='尚未配置查询凭证，请通过运营者提供的方式连接；不要在聊天中粘贴密钥。')
        if not 30<=len(token)<=128 or not re.fullmatch(r'[A-Za-z0-9_-]+',token):raise ValueError('invalid credential')
        headers['Authorization']='Bearer '+token
    req=request.Request(base_url+'/v1/'+endpoint,data=json.dumps(payload,ensure_ascii=False).encode(),headers=headers,method='POST')
    with request.build_opener(NoRedirect).open(req,timeout=15) as response:
        body=response.read(1024*1024+1)
        if len(body)>1024*1024:raise ValueError('response too large')
        data=json.loads(body)
    if data.get('status')!='ok':return dict(status='service_unavailable',message='共享查询暂不可用。')
    result=dict(status='ok',audience='public',as_of=data.get('as_of'))
    if args.command=='status':return dict(result,mode='http',search_available=bool(data.get('search_available')),client_latest=data.get('client_latest'),submission_available=bool(data.get('submission_available')),conversation_turn_available=bool(data.get('conversation_turn_available')),scheduler_available='由宿主另行核实')
    if args.command=='submit':return dict(result,submission_id=data.get('submission_id'),saved=data.get('saved') is True,registered=False,retention_days=data.get('retention_days'))
    if args.command=='stop-recording':return dict(result,recording_stopped=data.get('recording_stopped') is True)
    if args.command=='delete-submission':return dict(result,deleted=data.get('deleted') is True)
    if args.command=='stats':return dict(result,public_profile_count=data.get('public_profile_count'),resource_count=data.get('resource_count'),need_count=data.get('need_count'))
    return dict(result,results=[project_person(p) for p in data.get('results',[])[:args.limit]])

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',help='连接配置路径；不显示配置内容')
    sub=parser.add_subparsers(dest='command',required=True)
    sub.add_parser('status');sub.add_parser('stats')
    s=sub.add_parser('search');s.add_argument('--query',required=True);s.add_argument('--city',default='')
    s.add_argument('--kind',choices=['resource','need']);s.add_argument('--limit',type=int,default=3)
    p=sub.add_parser('person');p.add_argument('--id',required=True);p.set_defaults(limit=1)
    subm=sub.add_parser('submit');subm.add_argument('--file',required=True);subm.add_argument('--scope',choices=['profile_summary','conversation_excerpt','conversation_turn'],required=True);subm.add_argument('--id',required=True);subm.add_argument('--notice-shown',action='store_true',required=True)
    sub.add_parser('stop-recording')
    delete=sub.add_parser('delete-submission');delete.add_argument('--id',required=True)
    args=parser.parse_args()
    if hasattr(args,'limit'):args.limit=max(1,min(10,args.limit))
    if args.command=='search' and not args.query.strip():return dict(status='invalid_request',message='请先明确要找的需求或资源，不支持空查询枚举。')
    default_config=ROOT/'operator.local.json'
    if not default_config.is_file():default_config=Path.home()/'.config/xqg-entrepreneur-network/connection.json'
    path=Path(args.config or os.environ.get('XQG_NETWORK_CONFIG') or default_config).expanduser()
    if not path.is_file() and not args.config and not os.environ.get('XQG_NETWORK_CONFIG'):
        path=ROOT/'service.json'
    if not path.is_file():return dict(status='not_connected',search_available=False,message='尚未连接创业者资源网络。可以先整理档案卡，通过小强哥完成登记或申请引荐。')
    try:
        config=json.loads(path.read_text())
        if config.get('mode')=='ssh_operator':return ssh_operator(config,args)
        if config.get('mode')=='local':return local(config,args)
        if config.get('mode')=='http':return remote(config,args)
        return dict(status='configuration_error',message='未识别的连接方式。')
    except error.HTTPError as exc:
        if exc.code==429:
            return dict(status='query_limit_reached',message='查询频率或可查看档案额度已达上限。请稍后重试或联系小强哥，不通过换词、换账号或遍历编号绕过限制。')
        return dict(status='not_connected' if exc.code in (401,403) else 'service_unavailable',http_status=exc.code,message='查询未成功，请核实连接或权限。')
    except (OSError,ValueError,KeyError,TypeError,AttributeError,sqlite3.Error,error.URLError):
        return dict(status='service_unavailable',message='无法完成查询，请检查连接配置或服务状态；未返回任何匹配结果。')

if __name__=='__main__':
    print(json.dumps(main(),ensure_ascii=False,indent=2))
