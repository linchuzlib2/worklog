"""智谱 AI（GLM）客户端：对话 + 向量嵌入。"""
import os
import time

import requests

API_BASE = "https://open.bigmodel.cn/api/paas/v4"
CHAT_MODEL = os.getenv("ZHIPU_CHAT_MODEL", "glm-4.7-flash")
# 主模型持续 429（过载）时按顺序降级
CHAT_FALLBACKS = [m for m in os.getenv("ZHIPU_CHAT_FALLBACKS", "glm-4.6-flash,glm-4-flash,glm-4-flashx").split(",") if m.strip()]
EMBED_MODEL = os.getenv("ZHIPU_EMBED_MODEL", "embedding-3")
TIMEOUT = 60
# 429（限流/模型过载）与 5xx 为临时性错误，自动退避重试
RETRYABLE_CODES = {429, 500, 502, 503, 504}
MAX_RETRIES = 3
RETRY_DELAYS = (2, 5, 10)


class ZhipuError(Exception):
    pass


def api_key():
    return os.getenv("ZHIPU_API_KEY", "").strip()


def enabled():
    return bool(api_key())


def _post(path, payload):
    key = api_key()
    if not key:
        raise ZhipuError("尚未配置 ZHIPU_API_KEY，请在 Render 环境变量中填写智谱 API Key")
    last_error = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            response = requests.post(
                f"{API_BASE}/{path}",
                json=payload,
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                timeout=TIMEOUT,
            )
        except requests.RequestException as error:
            raise ZhipuError(f"请求智谱服务失败：{error}") from error
        if response.status_code == 200:
            break
        last_error = ZhipuError(f"智谱接口返回 {response.status_code}：{response.text[:300]}")
        if response.status_code not in RETRYABLE_CODES or attempt == MAX_RETRIES:
            raise last_error
        time.sleep(RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)])
    try:
        data = response.json()
    except ValueError as error:
        raise ZhipuError(f"智谱接口返回了无法解析的内容：{response.text[:200]}") from error
    if "choices" not in data and "data" not in data:
        raise ZhipuError(f"智谱接口响应异常：{str(data)[:300]}")
    return data


def chat(messages, temperature=0.3, max_tokens=2048):
    """对话补全，返回文本。主模型过载(429)时自动降级备用模型。"""
    models = [CHAT_MODEL] + CHAT_FALLBACKS
    for index, model in enumerate(models):
        try:
            data = _post("chat/completions", {
                "model": model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
            })
            return data["choices"][0]["message"]["content"]
        except ZhipuError as error:
            is_overload = "返回 429" in str(error)
            if not is_overload or index == len(models) - 1:
                raise
            # 主模型过载，换下一个备用模型


def embed(texts):
    """向量嵌入，输入字符串列表，返回向量列表。"""
    data = _post("embeddings", {"model": EMBED_MODEL, "input": texts})
    ordered = sorted(data["data"], key=lambda item: item["index"])
    return [item["embedding"] for item in ordered]
