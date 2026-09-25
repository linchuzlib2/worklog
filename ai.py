"""通用 OpenAI 兼容 AI 客户端。

默认对接 CodeBuddy Token Plan（腾讯 TokenHub），也可用于任何
OpenAI 兼容接口（智谱、DeepSeek、硅基流动等）——改环境变量即可。

环境变量：
  AI_API_BASE     接口地址，默认 https://tokenhub.tencentmaas.com/v1
  AI_API_KEY      API Key（TokenHub 的 API Key 管理页创建）
  AI_CHAT_MODEL   对话模型，默认 hy3
  AI_EMBED_MODEL  向量模型，默认为空 = 不用向量，检索降级为关键词匹配
"""
import json
import math
import os
import re
import time

import requests

TIMEOUT = 60
# 429（限流/过载）与 5xx 为临时性错误，自动退避重试
RETRYABLE_CODES = {429, 500, 502, 503, 504}
MAX_RETRIES = 3
RETRY_DELAYS = (2, 5, 10)


class AIError(Exception):
    pass


class EmbedUnavailable(AIError):
    """向量接口不可用（未配置模型或不支持），调用方应降级为关键词检索。"""


def api_key():
    return (os.getenv("AI_API_KEY") or os.getenv("ZHIPU_API_KEY") or "").strip()


def api_base():
    if os.getenv("AI_API_BASE"):
        return os.getenv("AI_API_BASE").rstrip("/")
    # 旧部署只配了智谱 Key 时，仍走智谱官方接口
    if os.getenv("AI_API_KEY"):
        return "https://tokenhub.tencentmaas.com/v1"
    if os.getenv("ZHIPU_API_KEY"):
        return "https://open.bigmodel.cn/api/paas/v4"
    return "https://tokenhub.tencentmaas.com/v1"


def chat_model():
    return os.getenv("AI_CHAT_MODEL", "glm-4.7-flash" if os.getenv("ZHIPU_API_KEY") and not os.getenv("AI_API_KEY") else "hy3")


def embed_model():
    return (os.getenv("AI_EMBED_MODEL") or ("embedding-3" if os.getenv("ZHIPU_API_KEY") and not os.getenv("AI_API_KEY") else "")).strip()


def enabled():
    return bool(api_key())


def _post(path, payload):
    key = api_key()
    if not key:
        raise AIError("尚未配置 AI_API_KEY，请在 Render 环境变量中填写（CodeBuddy Token Plan 在 TokenHub 控制台创建 API Key）")
    url = f"{api_base()}/{path}"
    for attempt in range(MAX_RETRIES + 1):
        try:
            response = requests.post(
                url,
                json=payload,
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                timeout=TIMEOUT,
            )
        except requests.RequestException as error:
            raise AIError(f"请求 AI 服务失败：{error}") from error
        if response.status_code == 200:
            break
        error = AIError(f"AI 接口返回 {response.status_code}：{response.text[:300]}")
        if response.status_code not in RETRYABLE_CODES or attempt == MAX_RETRIES:
            raise error
        time.sleep(RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)])
    try:
        data = response.json()
    except ValueError as error:
        raise AIError(f"AI 接口返回了无法解析的内容：{response.text[:200]}") from error
    if "choices" not in data and "data" not in data:
        raise AIError(f"AI 接口响应异常：{str(data)[:300]}")
    return data


def chat(messages, temperature=0.3, max_tokens=2048):
    """对话补全，返回文本。"""
    data = _post("chat/completions", {
        "model": chat_model(),
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    })
    return data["choices"][0]["message"]["content"]


def embed(texts):
    """向量嵌入。未配置向量模型或接口不支持时抛 EmbedUnavailable。"""
    model = embed_model()
    if not model:
        raise EmbedUnavailable("未配置 AI_EMBED_MODEL")
    try:
        data = _post("embeddings", {"model": model, "input": texts})
    except AIError as error:
        raise EmbedUnavailable(str(error)) from error
    try:
        ordered = sorted(data["data"], key=lambda item: item["index"])
        return [item["embedding"] for item in ordered]
    except (KeyError, TypeError) as error:
        raise EmbedUnavailable(f"向量接口响应异常：{error}") from error


# ---- 无向量接口时的关键词检索降级 ----

def tokenize(text):
    """中文按二元组，英文/数字按词，用于粗粒度相似度。"""
    text = text.lower()
    tokens = re.findall(r"[a-z0-9]+", text)
    cjk = re.sub(r"[^\u4e00-\u9fff]", "", text)
    tokens += [cjk[i:i + 2] for i in range(len(cjk) - 1)]
    return set(tokens)


def keyword_score(query_tokens, text_tokens):
    """两组 token 的重叠度（Jaccard 变体，长度归一化）。"""
    hits = len(query_tokens & text_tokens)
    return hits / math.sqrt(max(len(text_tokens), 1))
