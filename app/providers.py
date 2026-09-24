#!/usr/bin/env python3
"""LLM 厂商预设与配置解析。

纯数据 + 纯函数：不依赖 Flask，也不直接读写 config.json——配置字典由调用方传入，
因此可脱离 Web 层单独测试。server.py 负责读配置后调用 resolve()。
"""

from __future__ import annotations

DEFAULT_PROVIDER = "siliconflow"


def mask(v: str) -> str:
    """API Key 展示脱敏：只保留前几位，其余打码。"""
    v = (v or "").strip()
    if not v:
        return ""
    return (v[:6] + "****") if len(v) > 6 else (v[:2] + "****")


# 预设供应商：每个厂家独立配置（api_key / model / base_url 各存一份，互不覆盖）。
# base_url_field 缺省时回落到 base_url；context_window 仅作展示默认值；docs_url 为「前往官网/文档」。
LLM_PROVIDERS = {
    "deepseek": {"label": "DeepSeek", "logo": "🐳",
                 "desc": "deepseek · DeepSeek · OpenAI 兼容格式",
                 "base_url": "https://api.deepseek.com",
                 "api_key_field": "deepseek_api_key", "model_field": "deepseek_model",
                 "base_url_field": "deepseek_base_url",
                 "default_model": "deepseek-chat",
                 "models": ["deepseek-chat", "deepseek-reasoner"],
                 "context_window": 65536,
                 "docs_url": "https://api-docs.deepseek.com/zh-cn/",
                 "compute_url": "https://api.deepseek.com/chat/completions"},
    "siliconflow": {"label": "硅基流动 SiliconFlow", "logo": "🌟",
                    "desc": "siliconflow · 硅基流动 · OpenAI 兼容格式",
                    "base_url": "https://api.siliconflow.cn/v1",
                    "api_key_field": "api_key", "model_field": "model",
                    "base_url_field": "siliconflow_base_url",
                    "default_model": "Qwen/Qwen2.5-72B-Instruct",
                    "models": ["Qwen/Qwen2.5-72B-Instruct", "Qwen/Qwen2.5-32B-Instruct",
                               "Qwen/Qwen2.5-14B-Instruct", "Qwen/Qwen2.5-7B-Instruct",
                               "deepseek-ai/DeepSeek-V3", "deepseek-ai/DeepSeek-R1"],
                    "context_window": 65536,
                    "docs_url": "https://cloud.siliconflow.cn/account/ak",
                    "compute_url": "https://api.siliconflow.cn/v1/chat/completions"},
    "moonshot": {"label": "Kimi / Moonshot", "logo": "🌙",
                 "desc": "moonshot · Kimi · OpenAI 兼容格式",
                 "base_url": "https://api.moonshot.cn/v1",
                 "api_key_field": "moonshot_api_key", "model_field": "moonshot_model",
                 "base_url_field": "moonshot_base_url",
                 "default_model": "moonshot-v1-8k",
                 "models": ["moonshot-v1-8k", "moonshot-v1-32k", "moonshot-v1-128k"],
                 "context_window": 128000,
                 "docs_url": "https://platform.moonshot.cn/docs/",
                 "compute_url": "https://api.moonshot.cn/v1/chat/completions"},
    "dashscope": {"label": "阿里云百炼 DashScope", "logo": "☁️",
                  "desc": "dashscope · 阿里云百炼 · OpenAI 兼容格式",
                  "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                  "api_key_field": "dashscope_api_key", "model_field": "dashscope_model",
                  "base_url_field": "dashscope_base_url",
                  "default_model": "qwen-plus",
                  "models": ["qwen-max", "qwen-plus", "qwen-turbo", "qwen2.5-72b-instruct"],
                  "context_window": 128000,
                  "docs_url": "https://help.aliyun.com/zh/model-studio/",
                  "compute_url": "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"},
    "volcengine": {"label": "火山方舟", "logo": "🌋",
                   "desc": "volcengine · 火山方舟 · OpenAI 兼容格式",
                   "base_url": "https://ark.cn-beijing.volces.com/api/v3",
                   "api_key_field": "volcengine_api_key", "model_field": "volcengine_model",
                   "base_url_field": "volcengine_base_url",
                   "default_model": "doubao-pro-32k", "models": [],
                   "context_window": 32768,
                   "docs_url": "https://www.volcengine.com/product/ark",
                   "compute_url": "https://ark.cn-beijing.volces.com/api/v3/chat/completions"},
    "qianfan": {"label": "百度智能云千帆", "logo": "🦆",
                "desc": "qianfan · 百度千帆 · OpenAI 兼容格式",
                "base_url": "https://qianfan.baidubce.com/v2",
                "api_key_field": "qianfan_api_key", "model_field": "qianfan_model",
                "base_url_field": "qianfan_base_url",
                "default_model": "ernie-4.0-8k", "models": [],
                "context_window": 32768,
                "docs_url": "https://cloud.baidu.com/product/wenxinworkshop",
                "compute_url": "https://qianfan.baidubce.com/v2/chat/completions"},
    "stepfun": {"label": "阶跃星辰", "logo": "📶",
                "desc": "stepfun · 阶跃星辰 · OpenAI 兼容格式",
                "base_url": "https://api.stepfun.com/v1",
                "api_key_field": "stepfun_api_key", "model_field": "stepfun_model",
                "base_url_field": "stepfun_base_url",
                "default_model": "step-1-8k",
                "models": ["step-1-8k", "step-2-16k"],
                "context_window": 32768,
                "docs_url": "https://platform.stepfun.com/",
                "compute_url": "https://api.stepfun.com/v1/chat/completions"},
    "modelscope": {"label": "魔搭 ModelScope", "logo": "🧩",
                   "desc": "modelscope · 魔搭 · OpenAI 兼容格式",
                   "base_url": "https://api-inference.modelscope.cn/v1",
                   "api_key_field": "modelscope_api_key", "model_field": "modelscope_model",
                   "base_url_field": "modelscope_base_url",
                   "default_model": "qwen2.5-72b-instruct", "models": [],
                   "context_window": 32768,
                   "docs_url": "https://modelscope.cn/",
                   "compute_url": "https://api-inference.modelscope.cn/v1/chat/completions"},
    "sensenova": {"label": "商汤日日新 SenseNova", "logo": "🎨",
                  "desc": "sensenova · 商汤日日新 · OpenAI 兼容格式",
                  "base_url": "https://api.sensenova.cn/compatible-mode/v1",
                  "api_key_field": "sensenova_api_key", "model_field": "sensenova_model",
                  "base_url_field": "sensenova_base_url",
                  "default_model": "sensechat-5", "models": [],
                  "context_window": 32768,
                  "docs_url": "https://platform.sensenova.cn/",
                  "compute_url": "https://api.sensenova.cn/compatible-mode/v1/chat/completions"},
    "hunyuan": {"label": "腾讯混元", "logo": "💠",
                "desc": "hunyuan · 腾讯混元 · OpenAI 兼容格式",
                "base_url": "https://api.hunyuan.cloud.tencent.com/v1",
                "api_key_field": "hunyuan_api_key", "model_field": "hunyuan_model",
                "base_url_field": "hunyuan_base_url",
                "default_model": "hunyuan-turbo", "models": [],
                "context_window": 32768,
                "docs_url": "https://cloud.tencent.com/product/hunyuan",
                "compute_url": "https://api.hunyuan.cloud.tencent.com/v1/chat/completions"},
    "minimax": {"label": "MiniMax", "logo": "🅼",
                "desc": "minimax · MiniMax 开放平台 · OpenAI 兼容格式",
                "base_url": "https://api.minimax.chat/v1",
                "api_key_field": "minimax_api_key", "model_field": "minimax_model",
                "base_url_field": "minimax_base_url",
                "default_model": "MiniMax-Text-01", "models": [],
                "context_window": 32768,
                "docs_url": "https://platform.minimaxi.com/",
                "compute_url": "https://api.minimax.chat/v1/chat/completions"},
    "local": {"label": "本地部署 · Local", "logo": "🖥️",
              "desc": "local · OpenAI 兼容（任意 vLLM/Ollama/LM Studio 等）",
              "base_url": "", "api_key_field": "local_api_key", "model_field": "local_model",
              "base_url_field": "local_base_url",
              "default_model": "", "models": [], "context_window": 128000,
              "docs_url": "", "compute_url": ""},
    "custom": {"label": "自定义配置", "logo": "🛠️",
               "desc": "custom · 任意 OpenAI 兼容接口",
               "base_url": "", "api_key_field": "custom_api_key", "model_field": "custom_model",
               "base_url_field": "custom_base_url",
               "default_model": "", "models": [], "context_window": 128000,
               "docs_url": "", "compute_url": ""},
}

def normalize_provider(provider: str | None) -> str:
    """非法 / 未知的厂商 id 回落到默认厂商。"""
    return provider if provider in LLM_PROVIDERS else DEFAULT_PROVIDER


def resolve(cfg: dict) -> dict:
    """按当前配置的厂商，返回 base_url / api_key / model / label（各厂商字段独立，互不覆盖）。"""
    cfg = cfg or {}
    provider = normalize_provider(cfg.get("provider"))
    conf = LLM_PROVIDERS[provider]
    api_key = (cfg.get(conf["api_key_field"], "") or "").strip()
    model = (cfg.get(conf["model_field"], "") or "").strip() or conf["default_model"]
    saved = (cfg.get(conf.get("base_url_field") or "", "") or "").strip()
    base_url = (saved or conf["base_url"]).rstrip("/")
    return {"provider": provider, "label": conf["label"], "base_url": base_url,
            "api_key": api_key, "model": model}
