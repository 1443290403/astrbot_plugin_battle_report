"""Only local test certificates; never edit host Nginx or contact a certificate authority."""
from pathlib import Path
import shutil
import subprocess
import pytest
from scripts.setup_report_https import generate


def test_ip_certificate_chain_san_and_private_key_permissions(tmp_path):
    if not shutil.which('openssl'):pytest.skip('OpenSSL CLI unavailable')
    output=tmp_path/'ip-https'
    info=generate('127.0.0.1',output,port=8443)
    assert info['url']=='https://127.0.0.1:8443/battle-report/'
    subprocess.run(['openssl','verify','-CAfile',str(output/'ca.crt'),'-verify_ip','127.0.0.1',str(output/'server.crt')],check=True,capture_output=True)
    wrong=subprocess.run(['openssl','verify','-CAfile',str(output/'ca.crt'),'-verify_ip','127.0.0.2',str(output/'server.crt')],capture_output=True)
    assert wrong.returncode!=0
    for name in ['ca.key','server.key']:assert (output/name).stat().st_mode & 0o777==0o600
    config=(output/'battle-report-ip-https.conf').read_text()
    assert 'ssl_protocols TLSv1.2 TLSv1.3;' in config and 'proxy_set_header X-Forwarded-Proto https;' in config
    assert 'listen 8443 ssl;' in config and 'location / { return 404; }' in config
    before=(output/'ca.key').read_bytes()
    with pytest.raises(ValueError,match='非空'):generate('127.0.0.1',output)
    assert (output/'ca.key').read_bytes()==before


@pytest.mark.parametrize('ip,upstream,port',[
    ('bad','http://127.0.0.1:6185',8443),
    ('127.0.0.1','http://user:secret@127.0.0.1:6185',8443),
    ('127.0.0.1','http://127.0.0.1:6185/other',8443),
    ('127.0.0.1','http://bad;host:6185',8443),
    ('127.0.0.1','http://127.0.0.1:6185',0),
    ('127.0.0.1','http://8.8.8.8:6185',8443),
    ('127.0.0.1','http://127.0.0.1:0',8443),
])
def test_certificate_export_rejects_invalid_or_injectable_parameters_before_writing(tmp_path,ip,upstream,port):
    output=tmp_path/'not-created'
    with pytest.raises(ValueError):generate(ip,output,port=port,upstream=upstream)
    assert not output.exists()


def test_copied_standalone_script_runs_as_documented(tmp_path):
    if not shutil.which('openssl'):pytest.skip('OpenSSL CLI unavailable')
    import sys
    from scripts import setup_report_https
    source=tmp_path/'bin'/'setup_report_https.py';source.parent.mkdir()
    shutil.copyfile(setup_report_https.__file__,source)
    output=tmp_path/'certificates'
    result=subprocess.run([sys.executable,str(source),'--ip','127.0.0.1','--output',str(output),'--https-port','8443'],check=True,capture_output=True,text=True)
    assert 'https://127.0.0.1:8443/battle-report/' in result.stdout
    assert 'ca.crt 文件SHA256' in result.stdout
    assert (output/'server.crt').is_file()
