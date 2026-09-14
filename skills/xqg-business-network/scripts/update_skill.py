"""Check a pinned official release; install only after user approval, preserving local state."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import tempfile
from urllib import request

REPO='u5282261146-crypto/xqg-business-network'
PREFIX='skills/xqg-business-network/'
ROOT=Path(__file__).resolve().parents[1]
MAX_FILE=256*1024
class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self,*a,**k):return None

def digest(data):return hashlib.sha256(data).hexdigest()
def fetch(url):
    req=request.Request(url,headers={'User-Agent':'XQG-Skill-Updater','Accept':'application/json'})
    with request.build_opener(NoRedirect).open(req,timeout=15) as r:data=r.read(MAX_FILE+1)
    if len(data)>MAX_FILE:raise ValueError('release_file_too_large')
    return data

def allowed(name):
    if not isinstance(name,str) or '\\' in name:return False
    p=PurePosixPath(name)
    if str(p)!=name or p.is_absolute() or '..' in p.parts:return False
    if name in ('SKILL.md','LICENSE','service.json','agents/openai.yaml'):return True
    return bool(re.fullmatch(r'(references/[a-z0-9-]+\.md|scripts/[a-z0-9_]+\.py)',name))

def validate_manifest(m):
    if not isinstance(m,dict) or m.get('schema')!=1 or not re.fullmatch(r'\d+\.\d+\.\d+',str(m.get('version',''))):raise ValueError('invalid_manifest')
    if not isinstance(m.get('summary'),str) or len(m['summary'])>300:raise ValueError('invalid_manifest')
    f=m.get('files')
    if not isinstance(f,dict) or not 1<=len(f)<=40 or not {'SKILL.md','service.json','scripts/update_skill.py'}<=set(f):raise ValueError('invalid_manifest')
    for n,h in f.items():
        if not allowed(n) or not isinstance(h,str) or not re.fullmatch(r'[a-f0-9]{64}',h):raise ValueError('invalid_manifest_path_or_hash')
    return m

def release(commit=None):
    if commit is None:commit=json.loads(fetch('https://api.github.com/repos/'+REPO+'/commits/main'))['sha']
    if not isinstance(commit,str) or not re.fullmatch(r'[a-f0-9]{40}',commit):raise ValueError('invalid_commit')
    raw=fetch('https://raw.githubusercontent.com/'+REPO+'/'+commit+'/'+PREFIX+'release-manifest.json')
    return commit,validate_manifest(json.loads(raw)),raw

def safe_path(root,name):
    p=root/name
    for part in [p]+list(p.parents):
        if part==root.parent:break
        if part.is_symlink():raise ValueError('local_symlink_conflict')
    if p.exists() and not p.is_file():raise ValueError('local_path_conflict')
    return p

def baseline(root,manifest):
    p=safe_path(root,'release-manifest.json')
    if p.exists():return validate_manifest(json.loads(p.read_text()))['files']
    for old in manifest.get('legacy_baselines',[]):
        if not isinstance(old,dict) or not old or any(not allowed(n) for n in old):continue
        if all(safe_path(root,n).is_file() and digest((root/n).read_bytes())==h for n,h in old.items()):return old
    raise ValueError('local_changes_need_review')

def plan(root,m):
    old=baseline(root,m)
    conflicts=[];changes=[]
    for n,h in m['files'].items():
        p=safe_path(root,n);current=digest(p.read_bytes()) if p.exists() else None
        if current==h:continue
        if current is not None and old.get(n)!=current:conflicts.append(n)
        elif current is None and n in old:conflicts.append(n)
        else:changes.append(n)
    # Removed release files stay in place: never delete user files through an update.
    return changes,conflicts

def install(root,m,raw,contents,state_dir):
    changes,conflicts=plan(root,m)
    if conflicts:return dict(status='local_changes_need_review',files=conflicts,existing_service_available=True)
    for n in changes:
        if n not in contents or digest(contents[n])!=m['files'][n]:raise ValueError('release_hash_mismatch')
    if 'service.json' in contents:
        service=json.loads(contents['service.json'])
        if service!={'base_url':'https://api.xqgnetwork.com','automatic_session':True,'mode':'http'}:raise ValueError('connection_change_needs_review')
    for n in changes:
        if n.endswith('.py'):compile(contents[n],n,'exec')
    state_dir.mkdir(parents=True,exist_ok=True,mode=0o700)
    transaction=Path(tempfile.mkdtemp(prefix='update-',dir=state_dir))
    original={};written=[]
    for n in changes+['release-manifest.json']:
        p=safe_path(root,n);original[n]=p.read_bytes() if p.exists() else None
        if original[n] is not None:
            saved=transaction/n;saved.parent.mkdir(parents=True,exist_ok=True);saved.write_bytes(original[n]);saved.chmod(0o600)
    journal=root/'.update-pending.json'
    if journal.exists():raise ValueError('previous_update_needs_recovery')
    with journal.open('x') as f:json.dump({'backup':str(transaction),'files':{n:v is not None for n,v in original.items()}},f)
    try:
        for n in changes+['release-manifest.json']:
            target=safe_path(root,n);target.parent.mkdir(parents=True,exist_ok=True)
            data=raw if n=='release-manifest.json' else contents[n]
            fd,tmp=tempfile.mkstemp(prefix='.update-',dir=target.parent)
            try:
                with os.fdopen(fd,'wb') as f:f.write(data)
                written.append(n);os.replace(tmp,target)
            finally:
                if os.path.exists(tmp):os.unlink(tmp)
        journal.unlink()
    except Exception:
        for n in reversed(written):
            p=root/n
            if original[n] is None:p.unlink(missing_ok=True)
            else:p.write_bytes(original[n])
        journal.unlink(missing_ok=True)
        raise
    return dict(status='updated',version=m['version'],backup=str(transaction),changed_files=changes,reload_skill=True)

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root',type=Path,default=ROOT)
    p.add_argument('--commit');p.add_argument('--apply',action='store_true');p.add_argument('--approved',action='store_true')
    args=p.parse_args();root=args.root.absolute()
    if args.apply and (not args.approved or not args.commit):return dict(status='approval_required',message='先检查并取得用户更新确认，再使用检查返回的commit。')
    try:
        if (root/'.update-pending.json').exists():return dict(status='previous_update_needs_recovery',message='上次更新被中断，请助手按本地备份恢复后再更新。')
        commit,m,raw=release(args.commit)
        changes,conflicts=plan(root,m)
        if conflicts:return dict(status='local_changes_need_review',files=conflicts,existing_service_available=True)
        # Do not silently downgrade a newer local release.
        local=root/'release-manifest.json'
        if local.exists() and tuple(map(int,json.loads(local.read_text())['version'].split('.')))>tuple(map(int,m['version'].split('.'))):return dict(status='already_newer')
        if not args.apply:return dict(status='update_available' if changes else 'up_to_date',version=m['version'],summary=m['summary'],commit=commit,changed_files=changes,existing_service_available=True)
        contents={n:fetch('https://raw.githubusercontent.com/'+REPO+'/'+commit+'/'+PREFIX+n) for n in changes}
        result=install(root,m,raw,contents,Path.home()/'.config/xqg-entrepreneur-network/update-backups')
        return dict(result,commit=commit)
    except Exception as e:
        # Keep credentials, arbitrary server response bodies and paths out of errors.
        return dict(status='update_not_applied',reason=type(e).__name__,message='本次更新未完成；不要删除原Skill或连接配置。请助手检查网络、本地修改或更新备份。')
if __name__=='__main__':print(json.dumps(main(),ensure_ascii=False,indent=2))
