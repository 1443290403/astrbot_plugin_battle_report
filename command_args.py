"""QQ 命令参数解析：引号中的空格/逗号属于名字，不改写数据库里的 ID。

只在参数开头识别引号，因此 O'Connor 和反斜杠等原有 ID 保持原样。
支持英文单双引号、中文弯引号；引号内可用反斜杠转义引号或反斜杠。
"""
from dataclasses import dataclass

_QUOTES = {'"': '"', "'": "'", '“': '”', '‘': '’'}
_COMMAS = ',，、'


@dataclass(frozen=True)
class CommandArg:
    value: str
    quoted: bool
    start: int
    end: int


def parse_command_args(text: str, *, split_commas: bool = False,
                       split_whitespace: bool = True) -> list[CommandArg]:
    def separator(char: str) -> bool:
        return (split_whitespace and char.isspace()) or (split_commas and char in _COMMAS)

    result = []
    pos = 0
    while pos < len(text):
        while pos < len(text) and (text[pos].isspace() or separator(text[pos])):
            pos += 1
        if pos == len(text):
            break
        start = pos
        quoted = text[pos] in _QUOTES
        if quoted:
            closing = _QUOTES[text[pos]]
            pos += 1
            parts = []
            while pos < len(text) and text[pos] != closing:
                if text[pos] == '\\' and pos + 1 < len(text) and text[pos + 1] in (closing, '\\'):
                    pos += 1
                parts.append(text[pos])
                pos += 1
            if pos == len(text):
                raise ValueError('引号没有闭合喵，请把完整 ID 放在一对引号中，例如 "ice boundary"。')
            pos += 1
            value = ''.join(parts)
            if pos < len(text) and not text[pos].isspace() and not separator(text[pos]):
                raise ValueError('引号后的参数请用空格或逗号分隔喵。')
            if not split_whitespace:
                while pos < len(text) and text[pos].isspace():
                    pos += 1
                if pos < len(text) and not separator(text[pos]):
                    raise ValueError('多个 ID 请用逗号分隔喵。')
        else:
            while pos < len(text) and not separator(text[pos]):
                pos += 1
            value = text[start:pos].strip()
        if not value:
            raise ValueError('ID 或名字不能为空喵。')
        result.append(CommandArg(value, quoted, start, pos))
    return result


def single_name(text: str) -> str:
    """单名字命令兼容不加引号的完整名字，保留名字内部原有空格。"""
    args = parse_command_args(text)
    if any(arg.quoted for arg in args):
        if len(args) != 1:
            raise ValueError('此处只接受一个名字喵，带空格的名字请整体加引号。')
        return args[0].value
    return text.strip()


def parse_managed_ids(text: str) -> tuple[list[str], str]:
    """管理ID：最后一个参数是角色名，其余为逗号分隔 ID（兼容旧的裸空格 ID）。"""
    args = parse_command_args(text, split_commas=True)
    if len(args) < 2:
        raise ValueError('用法喵：/管理ID "ice boundary",红莲 "角色名字"')
    username = args[-1].value
    id_text = text[:args[-1].start].rstrip(' \t\r\n' + _COMMAS)
    ids = parse_command_args(id_text, split_commas=True, split_whitespace=False)
    if not ids:
        raise ValueError('请至少提供一个参赛ID喵。')
    return [arg.value for arg in ids], username
