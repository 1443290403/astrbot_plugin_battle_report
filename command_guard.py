"""识别 QQ 群命令意图，不执行插件过滤器或改写消息。"""

import re


def is_command_message(event, plain_type, registered_names):
    # 唤醒阶段可能已从 message_str 剥掉 /；原始 Plain 仍保留用户输入。
    # 只读顶层文字，不把 Reply 内被引用的命令当成当前指令。
    parts = [part.text for part in event.get_messages()
             if isinstance(part, plain_type) and isinstance(getattr(part, 'text', None), str)]
    text = ''.join(parts) if parts else event.get_message_str() or ''
    text = re.sub(r'\s+', ' ', text).strip()
    if not text:
        return False
    if text.startswith('/'):
        return True
    for name in registered_names():
        if not isinstance(name, str):
            continue
        name = re.sub(r'\s+', ' ', name).strip()
        if name and (text == name or text.startswith(name + ' ')):
            return True
    return False
