"""智谱 AI（GLM）客户端：对话 + 向量嵌入。"""
import os

import requests

API_BASE = "https://open.bigmodel.cn/api/paas/v4"
CHAT_MODEL = os.getenv("ZHIPU_CHAT_MODEL", "glm-4.7-flash")
EMBED_MODEL = os.getenv("ZHIPU_EMBED_MODEL", "embedding-3")
TIMEOUT = 60


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
    try:
        response = requests.post(
            f"{API_BASE}/{path}",
            json=payload,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            timeout=TIMEOUT,
        )
    except requests.RequestException as error:
        raise ZhipuError(f"请求智谱服务失败：{error}") from error
    if response.status_code != 200:
        raise ZhipuError(f"智谱接口返回 {response.status_code}：{response.text[:300]}")
    data = response.json()
    if "choices" not in data and "data" not in data:
        raise ZhipuError(f"智谱接口响应异常：{str(data)[:300]}")
    return data


def chat(messages, temperature=0.3, max_tokens=2048):
    """对话补全，返回文本。"""
    data = _post("chat/completions", {
        "model": CHAT_MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    })
    return data["choices"][0]["message"]["content"]


def embed(texts):
    """向量嵌入，输入字符串列表，返回向量列表。"""
    data = _post("embeddings", {"model": EMBED_MODEL, "input": texts})
    ordered = sorted(data["data"], key=lambda item: item["index"])
    return [item["embedding"] for item in ordered]
