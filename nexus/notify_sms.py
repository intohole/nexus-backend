from __future__ import annotations

import base64
import datetime
import hashlib
import hmac
import json
import os
import urllib.parse
import uuid
from typing import Any, Optional

import httpx

from nexus.logging import get_logger

logger = get_logger("nexus.notify_sms")

SMS_ENDPOINT = "https://dysmsapi.aliyuncs.com/"
SMS_VERSION = "2017-05-25"
SMS_ACTION = "SendSms"


def _rfc3986(value: str) -> str:
    return urllib.parse.quote(value, safe="")


def _sign_string(params: dict[str, str], access_key_secret: str) -> str:
    sorted_query = "&".join(
        f"{_rfc3986(k)}={_rfc3986(v)}" for k, v in sorted(params.items())
    )
    string_to_sign = f"GET&{_rfc3986('/')}&{_rfc3986(sorted_query)}"
    digest = hmac.new(
        (access_key_secret + "&").encode("utf-8"),
        string_to_sign.encode("utf-8"),
        hashlib.sha1,
    ).digest()
    return base64.b64encode(digest).decode("utf-8")


def _build_params(
    phone: str,
    template_code: str,
    template_param: Optional[dict[str, Any]],
    sign_name: str,
    access_key_id: str,
) -> dict[str, str]:
    now = datetime.datetime.now(datetime.timezone.utc)
    timestamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    params: dict[str, str] = {
        "AccessKeyId": access_key_id,
        "Action": SMS_ACTION,
        "Format": "JSON",
        "PhoneNumbers": phone,
        "RegionId": "cn-hangzhou",
        "SignatureMethod": "HMAC-SHA1",
        "SignatureNonce": uuid.uuid4().hex,
        "SignatureVersion": "1.0",
        "SignName": sign_name,
        "TemplateCode": template_code,
        "Timestamp": timestamp,
        "Version": SMS_VERSION,
    }
    if template_param:
        params["TemplateParam"] = json.dumps(template_param, ensure_ascii=False)
    return params


async def send_sms(
    phone: str,
    template_code: str,
    template_param: Optional[dict[str, Any]] = None,
    sign_name: str = "",
    access_key_id: str = "",
    access_key_secret: str = "",
) -> bool:
    ak_id: str = access_key_id or os.environ.get("ALIYUN_SMS_ACCESS_KEY_ID", "")
    ak_secret: str = access_key_secret or os.environ.get(
        "ALIYUN_SMS_ACCESS_KEY_SECRET", ""
    )
    sign_name = sign_name or os.environ.get("ALIYUN_SMS_SIGN_NAME", "")
    if not ak_id or not ak_secret or not sign_name:
        logger.warning(
            "阿里云短信未配置(ALIYUN_SMS_ACCESS_KEY_ID/SECRET/SIGN_NAME)，跳过短信发送"
        )
        return False
    params = _build_params(phone, template_code, template_param, sign_name, ak_id)
    params["Signature"] = _sign_string(params, ak_secret)
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(SMS_ENDPOINT, params=params)
            resp.raise_for_status()
            body = resp.json()
        if body.get("Code") == "OK":
            logger.info("短信发送成功: phone=%s", phone)
            return True
        logger.error(
            "短信发送失败: phone=%s code=%s message=%s",
            phone,
            body.get("Code"),
            body.get("Message"),
        )
        return False
    except Exception as exc:
        logger.error("短信发送异常: phone=%s error=%s", phone, str(exc))
        return False


__all__ = ["send_sms"]