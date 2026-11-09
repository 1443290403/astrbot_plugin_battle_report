"""独立前端导出边界：不覆盖现有目录，不携带后端文件或凭据。"""
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('report_export', ROOT / 'scripts/export_report_webui.py')
exporter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(exporter)


def test_export_is_static_only_and_preserves_embedded_page(tmp_path):
    source = ROOT / 'pages/report-comparison/index.html'
    before = source.read_bytes()
    target = tmp_path / 'site'
    version = exporter.export(target)
    assert {p.name for p in target.iterdir()} == {
        'index.html', 'app.js', 'style.css', 'standalone.js', 'standalone.css', 'VERSION',
    }
    html = (target / 'index.html').read_text('utf-8')
    assert html.index('./standalone.js') < html.index('./app.js')
    assert './standalone.css' in html
    assert (target / 'VERSION').read_text('utf-8').strip() == version
    assert (target / 'app.js').read_bytes() == (ROOT / 'pages/report-comparison/app.js').read_bytes()
    assert source.read_bytes() == before
    assert target.stat().st_mode & 0o777 == 0o755
    assert all(p.stat().st_mode & 0o777 == 0o644 for p in target.iterdir())


@pytest.mark.parametrize('existing', ['file', 'directory'])
def test_export_refuses_to_overwrite_user_files(tmp_path, existing):
    target = tmp_path / 'site'
    if existing == 'directory':
        target.mkdir()
        retained = target / 'my-config'
    else:
        retained = target
    retained.write_text('user data', 'utf-8')
    with pytest.raises(ValueError, match='不为空'):
        exporter.export(target)
    assert retained.read_text('utf-8') == 'user data'


def test_export_supports_empty_destination(tmp_path):
    target = tmp_path / 'empty'
    target.mkdir()
    exporter.export(target)
    assert (target / 'index.html').is_file()


def test_export_refuses_plugin_tree_including_symlink(tmp_path):
    alias = tmp_path / 'plugin-link'
    alias.symlink_to(ROOT, target_is_directory=True)
    for target in [ROOT, ROOT / 'public-output', alias / 'public-output']:
        with pytest.raises(ValueError, match='插件目录之外'):
            exporter.export(target)
    assert not (ROOT / 'public-output').exists()
