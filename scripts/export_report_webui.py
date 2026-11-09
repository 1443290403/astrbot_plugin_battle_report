"""导出自用独立战报前端；只复制静态资源，不导出数据库配置或凭据。"""
import argparse
from pathlib import Path
import re
import shutil
import tempfile

ROOT=Path(__file__).resolve().parents[1]


def export(output: Path, *, root: Path=ROOT):
    root=root.resolve();output=output.expanduser().resolve()
    if output.is_relative_to(root):
        raise ValueError('请导出到插件目录之外，避免更新插件时覆盖站点。')
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise ValueError('输出目录已存在且不为空，请使用新的目录。')
    output.parent.mkdir(parents=True,exist_ok=True)
    version=re.search(r'^version:\s*(\S+)',(root/'metadata.yaml').read_text('utf-8'),re.M).group(1)
    with tempfile.TemporaryDirectory(prefix='.battle-report-web-',dir=output.parent) as temporary:
        stage=Path(temporary)/'site';stage.mkdir()
        page=root/'pages/report-comparison'
        html=(page/'index.html').read_text('utf-8')
        marker='<script type="module" src="./app.js"></script>'
        if html.count(marker)!=1:raise ValueError('页面脚本入口不符合预期。')
        html=html.replace('</head>','<link rel="stylesheet" href="./standalone.css">\n</head>',1)
        html=html.replace(marker,'<script src="./standalone.js"></script>\n'+marker,1)
        (stage/'index.html').write_text(html,'utf-8')
        for filename in ('app.js','style.css'):shutil.copyfile(page/filename,stage/filename)
        for filename in ('standalone.js','standalone.css'):shutil.copyfile(root/'webui_standalone'/filename,stage/filename)
        (stage/'VERSION').write_text(version+'\n','utf-8')
        # Nginx workers commonly run as a different user, including after docker cp.
        # These files are public static assets only; never export backend config here.
        for asset in stage.iterdir():asset.chmod(0o644)
        stage.chmod(0o755)
        if output.exists():output.rmdir()  # Only the verified empty destination is removed.
        stage.rename(output)
    return version


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True,help='新建或为空的站点目录，必须位于插件目录外')
    args=parser.parse_args()
    try:version=export(args.output)
    except (ValueError,OSError) as error:parser.exit(1,f'导出失败：{error}\n')
    print(f'已导出独立战报前端 v{version}：{args.output.resolve()}')
    print('需通过同源Nginx代理AstrBot鉴权和插件API；请查看STANDALONE_WEBUI_GUIDE.md。')


if __name__=='__main__':main()
