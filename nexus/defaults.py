"""内部基建服务的默认地址与共享常量，全工作区唯一权威来源。"""

DEFAULT_MINIDEPLOY_BASE_URL = "http://10.100.0.1:8900"
DEFAULT_NOTIFY_CENTER_URL = "http://10.100.0.1:8910"
DEFAULT_UC_BASE_URL = "http://10.100.0.1:8901"

# 服务发现不可达且无磁盘快照时的引导兜底（仅 core infra，业务应用不进表）。
# 值须与生产注册表对表，解析链与豁免见 handoff/17「服务端点解析契约」。
BOOTSTRAP_SERVICE_URLS: dict[str, str] = {
    "beememory": "http://10.100.0.1:8700",
    "lion": "http://10.100.0.1:9527",
    "moutain": "http://10.100.0.1:8250",
    "notifycenter": DEFAULT_NOTIFY_CENTER_URL,
    "promptmanager": "http://10.100.0.1:8400",
    "usercenter": DEFAULT_UC_BASE_URL,
    "userfeedback": "http://10.100.0.1:8100",
    "wanxiang": "http://10.100.0.4:8001",
}
