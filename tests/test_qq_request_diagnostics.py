"""服务器诊断脚本：字段脱敏、只读、Docker 定位和失败信息隔离。"""
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import diagnose_qq_requests as diagnostics


def make_store(tmp_path, records):
    path = tmp_path / 'plugin_data' / 'battle_report' / 'request_reviews.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'version': 1, 'requests': records}), encoding='utf-8')
    return path


def record(flag='slreq:1:765:30003:2:0', **extra):
    return {'kind': 'group', 'status': 'pending', 'source': 'snowluma_invite_card',
            'request_time': 100, 'created_at': 101.5, 'flag': flag,
            'self_id': '12345', 'group_id': '30003', 'user_id': '20002',
            'comment': 'PRIVATE_COMMENT', 'notification_text': 'PRIVATE_NOTICE',
            'unrelated_config': {'password': 'PRIVATE_PASSWORD'}, **extra}


def test_report_keeps_approval_tuple_and_removes_personal_data(tmp_path):
    path = make_store(tmp_path, {'ABCDEF01': record()})
    before = path.read_bytes()
    report = diagnostics.read_store(path)
    row = report['records'][0]
    assert row['token'] == 'ABCDEF01' and row['source'] == 'snowluma_invite_card'
    assert row['request_time'] == 100
    assert row['flag'] == {'format': 'slreq:1', 'sequence': '765', 'event_type': 2,
        'filtered': False, 'group_matches_record': True,
        'fingerprint': diagnostics.fingerprint('slreq:1:765:30003:2:0')}
    rendered = json.dumps(report)
    for secret in ['12345', '30003', '20002', 'PRIVATE_COMMENT', 'PRIVATE_NOTICE', 'PRIVATE_PASSWORD']:
        assert secret not in rendered
    assert path.read_bytes() == before


@pytest.mark.parametrize('flag,kind', [('invite:30003:u_PRIVATE', 'legacy_invite'),
    ('add:30003:u_PRIVATE', 'legacy_add'), ('opaque_PRIVATE', 'opaque'), ('765', 'sequence')])
def test_other_flag_forms_never_export_opaque_handles(flag, kind):
    detail = diagnostics.flag_details(flag, '30003')
    assert detail['format'] == kind
    assert 'PRIVATE' not in json.dumps(detail) and '30003' not in json.dumps(detail)


def test_detects_wrong_group_without_exporting_group_number():
    detail = diagnostics.flag_details('slreq:1:765:30004:2:0', '30003')
    assert detail['group_matches_record'] is False
    assert '30004' not in json.dumps(detail)


def test_missing_source_old_records_and_malformed_values_do_not_crash(tmp_path):
    old = record(status=[], request_time='PRIVATE_TIME', created_at=float('nan'), flag=None)
    old.pop('source')
    path = make_store(tmp_path, {'ABCDEF01': old, 'INVALID_TOKEN_PRIVATE': record()})
    rows = diagnostics.read_store(path)['records']
    assert len(rows) == 1 and rows[0]['source'] == 'absent'
    assert rows[0]['status'] == 'unknown' and rows[0]['request_time'] is None
    assert rows[0]['created_at'] is None and rows[0]['flag'] == {'format': 'invalid'}
    assert 'PRIVATE' not in json.dumps(rows)


def test_selects_newest_or_exact_token_and_deduplicates_paths(tmp_path):
    path = make_store(tmp_path, {'ABCDEF01': record(created_at=101),
                                 'ABCDEF02': record(created_at=102)})
    reports = diagnostics.local_stores([path, path.parent.parent], limit=1)
    assert len(reports) == 1 and reports[0]['records'][0]['token'] == 'ABCDEF02'
    assert diagnostics.read_store(path, token='abcdef01')['records'][0]['token'] == 'ABCDEF01'


def test_corrupt_oversized_or_other_files_are_not_dumped(tmp_path, monkeypatch):
    path = make_store(tmp_path, {})
    path.write_text('PRIVATE_BROKEN_JSON', encoding='utf-8')
    assert diagnostics.read_store(path)['error'] == 'store_unreadable_or_invalid'
    monkeypatch.setattr(diagnostics, 'MAX_BYTES', 4)
    assert diagnostics.read_store(path)['error'] == 'store_too_large'
    other = tmp_path / 'config.json'
    other.write_text('PRIVATE_PASSWORD', encoding='utf-8')
    assert diagnostics.read_store(other)['error'] == 'not_a_request_store'
    assert path.read_text() == 'PRIVATE_BROKEN_JSON'


def test_docker_discovery_prefers_astrbot_and_runs_only_reader(monkeypatch):
    calls = []
    monkeypatch.setattr(diagnostics.shutil, 'which', lambda _: '/usr/bin/docker')
    def run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        if cmd[:2] == ['docker', 'ps']:
            return SimpleNamespace(returncode=0, stdout='db1\tmysql:8\tdb\na1\tastrbot:4\tbot\n')
        assert cmd[:6] == ['docker', 'exec', '-i', 'a1', 'python3', '-']
        assert '--worker' in cmd and '--token' in cmd and 'ABCDEF01' in cmd
        assert kwargs['input'] == Path(diagnostics.__file__).read_text(encoding='utf-8')
        return SimpleNamespace(returncode=0, stdout=json.dumps({'stores': [{'records': []}]}))
    monkeypatch.setattr(diagnostics.subprocess, 'run', run)
    stores, notes = diagnostics.docker_stores(token='ABCDEF01')
    assert stores == [{'records': [], 'container': 'a1'}] and notes == []
    assert len(calls) == 2


@pytest.mark.parametrize('failure', ['invalid_json', 'failed_process', 'timeout'])
def test_docker_errors_never_echo_arbitrary_process_output(monkeypatch, failure):
    monkeypatch.setattr(diagnostics.shutil, 'which', lambda _: '/usr/bin/docker')
    def run(cmd, **kwargs):
        if failure == 'timeout':
            raise subprocess.TimeoutExpired(cmd, 1, stderr='PRIVATE_PASSWORD')
        return SimpleNamespace(returncode=1 if failure == 'failed_process' else 0,
                               stdout='PRIVATE_PASSWORD', stderr='PRIVATE_PASSWORD')
    monkeypatch.setattr(diagnostics.subprocess, 'run', run)
    report = diagnostics.docker_stores(container='a1')
    assert 'PRIVATE_PASSWORD' not in json.dumps(report) and report[0] == [] and report[1]


def test_container_worker_does_not_scan_docker_or_modify_store(tmp_path, monkeypatch, capsys):
    path = make_store(tmp_path, {'ABCDEF01': record()})
    before = path.read_bytes()
    monkeypatch.setattr(diagnostics, 'docker_stores', lambda *a: pytest.fail('worker must not call Docker'))
    assert diagnostics.main(['--worker', '--path', str(path), '--token', 'ABCDEF01']) == 0
    report = json.loads(capsys.readouterr().out)
    assert report['stores'][0]['records'][0]['token'] == 'ABCDEF01'
    assert path.read_bytes() == before
