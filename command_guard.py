"""识别 QQ 群命令意图，不执行插件过滤器或改写消息。"""

import re


def message_text(event, plain_type):
    # 唤醒阶段可能已从 message_str 剥掉 /；原始 Plain 仍保留用户输入。
    # 只读顶层文字，不把 Reply 内被引用的命令当成当前指令。
    parts = [part.text for part in event.get_messages()
             if isinstance(part, plain_type) and isinstance(getattr(part, 'text', None), str)]
    text = ''.join(parts) if parts else event.get_message_str() or ''
    return text


def command_candidates(event, plain_type, wake_prefixes=()):
    raw = message_text(event, plain_type).strip()
    yield raw
    for prefix in wake_prefixes:
        if isinstance(prefix, str) and prefix and raw.startswith(prefix):
            yield raw[len(prefix):].strip()


def is_command_message(event, plain_type, registered_names, wake_prefixes=()):
    names = None
    for raw in command_candidates(event, plain_type, wake_prefixes):
        text = re.sub(r'\s+', ' ', raw).strip()
        if not text:
            continue
        if text.startswith('/'):
            return True
        if names is None:
            names = [re.sub(r'\s+', ' ', name).strip()
                     for name in registered_names() if isinstance(name, str) and name.strip()]
        if any(text == name or text.startswith(name + ' ') for name in names):
            return True
    return False
