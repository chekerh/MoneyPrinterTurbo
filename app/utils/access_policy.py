"""启动时的监听地址策略：默认只允许回环，非回环必须显式开启。

这台机器同时持有局域网地址，而 ``api_key`` 默认为空（等于没有鉴权）。在这种
组合下把监听地址设成 ``0.0.0.0``，等于把任务、脚本和生成产物暴露给同一网络里的
任何人。

为什么用 stdlib 的 ``ipaddress`` 而不是字符串前缀判断：``"127.0.0.1.evil.com"``
这类字符串用前缀匹配会被误判为回环，而它实际会解析到别处。判定必须基于解析
结果；解析不出来就当作不安全。
"""

from __future__ import annotations

import ipaddress
import socket

__all__ = [
    "AccessPolicyError",
    "is_loopback_host",
    "resolve_bind_host",
]


class AccessPolicyError(RuntimeError):
    """监听地址不满足访问策略。"""


def is_loopback_host(host: object) -> bool:
    """判断监听地址是否只绑定回环接口。

    只认两种写法：IP 字面量，以及解析后确实指向回环的本地主机名。解析失败
    一律返回 ``False``——宁可拒绝启动，也不要在判断不了的时候放行。
    """
    if not isinstance(host, str):
        return False
    text = host.strip()
    if not text:
        return False

    try:
        return ipaddress.ip_address(text).is_loopback
    except ValueError:
        pass

    # "localhost" 按定义就是本机，直接认可，不再交给解析器——有些环境里
    # 它并不在 /etc/hosts 中。
    if text.lower() == "localhost":
        return True

    try:
        resolved = socket.gethostbyname(text)
    except (OSError, UnicodeError):
        return False
    try:
        return ipaddress.ip_address(resolved).is_loopback
    except ValueError:
        return False


def resolve_bind_host(listen_host: object, allow_non_loopback: bool = False) -> str:
    """
    返回可以安全使用的监听地址，不满足策略则抛出 :class:`AccessPolicyError`。

    :param listen_host: 配置里的监听地址。
    :param allow_non_loopback: 用户是否显式开启了非回环绑定。开启后责任自负：
        配合默认的空 ``api_key``，这意味着同一网络内任何人可访问。
    """
    text = str(listen_host).strip() if listen_host is not None else ""
    if not text:
        raise AccessPolicyError("listen_host 未配置")

    if is_loopback_host(text):
        return text

    if not allow_non_loopback:
        raise AccessPolicyError(
            f"拒绝以非回环地址 {text} 启动：api_key 默认为空，这会把任务与产物"
            f"暴露给同一网络内的任何人。确认要开放后，在 config.toml 中设置"
            f" allow_non_loopback_bind = true 并自行配置 api_key。"
        )

    return text
