# Connection storage keeps the pre-rename path to preserve existing identities.
"""Save a local connection; credentials stay in a separate local file."""
import argparse,json,os,re
from pathlib import Path
from urllib.parse import urlsplit

def save(url,credential_file,output):
    u=urlsplit(url)
    if u.scheme!='https' or not u.hostname or u.username or u.password or u.query or u.fragment:raise ValueError('需要小强哥提供的 HTTPS 地址')
    credential_file=Path(credential_file).expanduser().resolve()
    if os.name=='posix' and credential_file.stat().st_mode & 0o077:raise ValueError('请先将凭证文件权限设为600')
    token=credential_file.read_text().strip()
    if not re.fullmatch(r'[A-Za-z0-9_-]{30,128}',token):raise ValueError('凭证格式不正确')
    output=Path(output).expanduser();output.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    with output.open('x') as f:
        output.chmod(0o600)
        json.dump({'mode':'http','base_url':url.rstrip('/'),'token_file':str(credential_file)},f,ensure_ascii=False,indent=2)
    return str(output)

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--url',required=True)
    p.add_argument('--credential-file',type=Path,required=True)
    p.add_argument('--output',type=Path,default=Path.home()/'.config/xqg-entrepreneur-network/connection.json')
    a=p.parse_args()
    try:print(json.dumps({'status':'configured_not_yet_verified','config':save(a.url,a.credential_file,a.output)},ensure_ascii=False))
    except (OSError,ValueError) as e:print(json.dumps({'status':'configuration_error','message':str(e)},ensure_ascii=False));raise SystemExit(1)
