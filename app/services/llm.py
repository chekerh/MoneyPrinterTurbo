import json
import logging
import random as _random
import re
import time as _time
from time import perf_counter
from typing import List

from loguru import logger
from openai import AzureOpenAI, OpenAI
from openai.types.chat import ChatCompletion

from app.config import config
from app.models.llm_provider import DEFAULT_LLM_PROVIDER_ID, get_llm_provider

_max_retries = max(1, int(config.app.get("llm_max_retries", 8)))
MIN_SCRIPT_PARAGRAPH_NUMBER = 1
MAX_SCRIPT_PARAGRAPH_NUMBER = 10
MAX_SCRIPT_PROMPT_LENGTH = 2000
MAX_SCRIPT_SYSTEM_PROMPT_LENGTH = 8000
_transient_retry_seconds_baseline = max(1, int(config.app.get("llm_retry_baseline_seconds", 5)))
_transient_retry_seconds_cap = max(10, int(config.app.get("llm_retry_cap_seconds", 120)))
_transient_status_codes = {408, 429, 500, 502, 503, 504}
_transient_error_markers = (
    "high demand",
    "try again",
    "rate limit",
    "quota",
    "resource exhausted",
    "resource_exhausted",
    "unavailable",
    "temporarily",
)
# 429 with these markers is a permanent billing/exhaustion state, not a throttle:
# retrying it only burns backoff time. Anything else on 429 stays transient.
_permanent_429_markers = (
    "insufficient_quota",
    "insufficient quota",
    "no quota",
    "no remaining quota",
    "billing hard limit",
    "billing limit",
    "payment required",
    "no active account balance",
    "no credits",
)
_THINK_BLOCK_RE = re.compile(r"<think\b[^>]*>.*?</think>", re.IGNORECASE | re.DOTALL)
_UNCLOSED_THINK_BLOCK_RE = re.compile(r"<think\b[^>]*>.*$", re.IGNORECASE | re.DOTALL)
_URL_USERINFO_RE = re.compile(
    r"((?:https?|wss?)://)([^/\s?#@]*:[^/\s?#@]*@)", re.IGNORECASE
)
_SENSITIVE_QUERY_RE = re.compile(
    r"([?&](?:api[_-]?key|access[_-]?token|token|key|secret|password)=)([^&#\s]+)",
    re.IGNORECASE,
)

DEFAULT_SCRIPT_SYSTEM_PROMPT = """
# Role: Video Script Generator

## Goals:
Generate a script for a video, depending on the subject of the video.

## Constrains:
1. the script is to be returned as a string with the specified number of paragraphs.
2. do not under any circumstance reference this prompt in your response.
3. get straight to the point. never open with "welcome to this video", a greeting,
   or any stage-setting. when a channel brand line is specified below, it goes after
   the hook, never in place of it.
4. you must not include any type of markdown or formatting in the script, never use a title.
5. only return the raw content of the script.
6. do not include "voiceover", "narrator" or similar indicators of what should be spoken at the beginning of each paragraph or line.
7. you must not mention the prompt, or anything about the script itself. also, never talk about the amount of paragraphs or lines. just write the script.
8. respond in the same language as the video subject.

## Retention (hook first):
- open with the hook: state the problem and promise the payoff in the first line.
- no run-up. strip "let's dive in", "here's what you need to know", "the thing is",
  and "in this video". the first line is the hook, and it arrives before any
  greeting or channel identification.
- deliver a payoff before attention wanes; every next sentence must feel earned.
- tight pacing: no "dead air" filler sentences. treat the script like a short-form
  edit where every frame must earn its place.

## Voice:
Write like a human host speaking to one specific viewer, not a chatbot writing for
everyone. choices should be uneven and specific, not the most average phrasing.
vary sentence length: alternate short and long sentences. read each line aloud;
it must be comfortable to speak and must add something the viewer did not already know.

## Banned AI patterns (humanizer):
- no "not X but Y" contrasts, no one-line closers ("that is the real win", "let that
  sink in"), and no arguing with no one ("this isn't about X", "to be clear").
- no forced triads ("innovation, inspiration, insights") unless the meaning truly
  has three parts.
- no em dashes or en dashes; use periods, commas, or parentheses instead.
- no inflated words: actually, additionally, enhance, fostering, pivotal, showcase,
  testament, underscore, vibrant, delve, robust, rich.
- no inflated significance ("stands as a testament", "the future looks bright").
- no sales language: boasts, nestled, stunning, breathtaking, must-visit.
- no chatbot residue ("I hope this helps", "let me know", "great question").
- no bold labels, headings, emojis, bullets, or decorative formatting.
- no sayings that pose as depth: "the real question is", "at its core", "what
  really matters", "the deeper issue", "the heart of the matter", "X is the Y
  of Z". state the specific claim instead.
- no arguing with nobody: "to be clear", "don't get me wrong", "this isn't
  mainly about", "I'm not saying", "one might be tempted to". delete the
  objection nobody made.
- no knowledge-limit hedging: "as of", "up to my last update", "it is believed
  that", "not publicly available". either state what is known or leave it out.
  never present a guess as fact.
- use "is", "are", "has" rather than "serves as", "stands as", "functions as",
  "boasts", "features", "offers", "maintains", "refers to".
- no "-ing" rider bolted onto a plain fact (highlighting, underscoring,
  showcasing, fostering, ensuring) unless it carries real meaning.
- no unnamed authority: "experts argue", "studies show" without naming who.
  if no source exists, do not invent one.
- vary sentence openings. do not start three sentences the same way.
""".strip()


def _normalize_text_response(content, llm_provider: str) -> str:
    # 不同 LLM SDK 在异常或被拦截场景下，可能返回 None、空字符串，
    # 甚至返回非字符串对象。这里统一做兜底校验，避免后续直接调用
    # `.replace()` 时抛出 `NoneType` 之类的属性错误。
    if content is None:
        raise ValueError(f"[{llm_provider}] returned empty text content")

    if not isinstance(content, str):
        raise TypeError(
            f"[{llm_provider}] returned non-text content: {type(content).__name__}"
        )

    # MiniMax M3、DeepSeek R1 这类 reasoning 模型可能会把内部推理包在
    # `<think>...</think>` 中返回。视频脚本和关键词只需要最终可朗读文本，
    # 如果不在服务层统一清理，WebUI、字幕和配音都会把思考过程当正文处理。
    content = _THINK_BLOCK_RE.sub("", content)
    content = _UNCLOSED_THINK_BLOCK_RE.sub("", content).strip()
    if not content:
        raise ValueError(f"[{llm_provider}] returned empty text content")

    # 前面的 ``strip()`` 已经清理首尾空白。这里必须保留正文中的单换行和
    # 双换行：脚本生成依赖双换行区分段落，字幕处理也会按行读取用户文案。
    return content


def _sanitize_error_message(error: object) -> str:
    """
    清理返回给 WebUI/API 的错误信息，避免自定义 base_url 中的凭据泄露。

    一些 OpenAI-compatible SDK 会把请求 URL 原样拼进异常信息。如果用户为了
    代理网关配置了 `https://user:pass@example.com/v1`，直接返回 `str(e)`
    就会把密码暴露给页面、API 调用方或后续日志。这里仅处理错误文案，不改变
    实际请求地址，避免影响正常调用链路。
    """
    message = str(error)
    message = _URL_USERINFO_RE.sub(r"\1***:***@", message)
    message = _SENSITIVE_QUERY_RE.sub(r"\1***", message)
    return message


def _extract_chat_completion_text(response, llm_provider: str) -> str:
    # OpenAI 兼容接口在异常场景下，可能返回没有 choices、
    # 或者 choices/message/content 为空的响应对象。
    # 这里统一做结构校验，避免出现 `NoneType is not subscriptable`
    # 这类底层属性访问错误。
    choices = getattr(response, "choices", None)
    if not choices:
        raise ValueError(f"[{llm_provider}] returned empty choices")

    first_choice = choices[0]
    message = getattr(first_choice, "message", None)
    if message is None:
        raise ValueError(f"[{llm_provider}] returned empty message")

    content = getattr(message, "content", None)
    return _normalize_text_response(content, llm_provider)


def _get_response_field(value, key: str):
    """兼容 dict 和 SDK 响应对象的字段读取。"""
    if isinstance(value, dict):
        return value.get(key)

    try:
        return value[key]
    except (KeyError, TypeError, AttributeError):
        return getattr(value, key, None)


def _extract_qwen_generation_text(response) -> str:
    """
    从 DashScope Generation 响应中提取文本。

    Qwen 使用 `messages` 调用时返回的是 chat 结构：
    `output.choices[0].message.content`；旧 completion 形态才会返回
    `output.text`。这里两个路径都兼容，避免 `output.text` 为 None 时
    继续 `.replace()` 触发不可诊断的 AttributeError。
    """
    output = _get_response_field(response, "output")
    choices = _get_response_field(output, "choices") if output else None
    if choices is not None:
        if not choices:
            logger.warning("Qwen returned an empty choices list")
            raise ValueError("[qwen] returned empty choices")

        first_choice = choices[0]
        message = _get_response_field(first_choice, "message")
        content = _get_response_field(message, "content") if message else None
        if content is not None:
            return _normalize_text_response(content, "qwen")

    text = _get_response_field(output, "text") if output else None
    return _normalize_text_response(text, "qwen")


def _generate_response_once(prompt: str, app_config=None) -> str:
    try:
        # WebUI 在视频生成期间允许用户准备下一条文案。调用方可以传入提交瞬间
        # 的配置快照，确保模型请求重试期间不会因为后台任务结束并应用新配置，
        # 而切换到另一个 Provider、Base URL 或模型。
        runtime_app_config = app_config if app_config is not None else config.app
        llm_provider = str(
            runtime_app_config.get("llm_provider", DEFAULT_LLM_PROVIDER_ID)
        ).lower()
        provider = get_llm_provider(llm_provider)
        if provider is None:
            raise ValueError(f"{llm_provider}: unsupported llm provider")

        logger.info(f"llm provider: {llm_provider}")
        api_key = runtime_app_config.get(provider.config_key("api_key"), "")
        configured_model = runtime_app_config.get(provider.config_key("model_name"), "")
        model_name = provider.resolve_model_name(configured_model)
        if configured_model and model_name != configured_model:
            logger.warning(
                f"{llm_provider} model '{configured_model}' is deprecated, "
                f"fallback to '{model_name}'"
            )
        configured_base_url = runtime_app_config.get(
            provider.config_key("base_url"), ""
        )
        base_url = provider.resolve_base_url(configured_base_url)
        if configured_base_url and configured_base_url.strip().rstrip("/") in {
            url.rstrip("/") for url in provider.deprecated_base_urls
        }:
            logger.warning(
                f"{llm_provider} base URL '{configured_base_url}' is deprecated, "
                f"fallback to '{base_url}'"
            )
        adapter = provider.adapter
        api_version = ""

        # Ollama 的默认地址依赖当前是否运行在容器中，无法作为静态 Registry
        # 值保存；Registry 仍负责模型和必填规则，运行环境差异在这里解析。
        if llm_provider == "ollama":
            api_key = "ollama"
            if not base_url:
                base_url = config.get_default_ollama_base_url()

        if adapter == "azure":
            api_version = runtime_app_config.get(
                provider.config_key("api_version"), "2024-02-15-preview"
            )

        extra_values = {
            field.config_suffix: (
                runtime_app_config.get(provider.config_key(field.config_suffix), "")
                or field.default_value
            )
            for field in provider.extra_fields
        }

        if provider.requires_api_key and not api_key:
            raise ValueError(
                f"{llm_provider}: api_key is not set, please set it in the config.toml file."
            )
        if provider.requires_model_name and not model_name:
            raise ValueError(
                f"{llm_provider}: model_name is not set, please set it in the config.toml file."
            )
        if provider.requires_base_url and not base_url:
            raise ValueError(
                f"{llm_provider}: base_url is not set, please set it in the config.toml file."
            )

        for field in provider.extra_fields:
            if field.required and not extra_values[field.config_suffix]:
                raise ValueError(
                    f"{llm_provider}: {field.config_suffix} is not set, "
                    "please set it in the config.toml file."
                )

        if adapter == "qwen":
            import dashscope
            from dashscope.api_entities.dashscope_response import GenerationResponse

            dashscope.api_key = api_key
            response = dashscope.Generation.call(
                model=model_name, messages=[{"role": "user", "content": prompt}]
            )
            if response:
                if isinstance(response, GenerationResponse):
                    status_code = response.status_code
                    if status_code != 200:
                        raise Exception(
                            f'[{llm_provider}] returned an error response: "{response}"'
                        )

                    return _extract_qwen_generation_text(response)
                else:
                    raise Exception(
                        f'[{llm_provider}] returned an invalid response: "{response}"'
                    )
            else:
                raise Exception(f"[{llm_provider}] returned an empty response")

        if adapter == "gemini":
            from google import genai
            from google.genai import types

            http_options = types.HttpOptions(base_url=base_url) if base_url else None
            generation_config = types.GenerateContentConfig(
                temperature=0.5,
                top_p=1,
                top_k=1,
                max_output_tokens=2048,
                safety_settings=[
                    types.SafetySetting(
                        category="HARM_CATEGORY_HARASSMENT",
                        threshold="BLOCK_ONLY_HIGH",
                    ),
                    types.SafetySetting(
                        category="HARM_CATEGORY_HATE_SPEECH",
                        threshold="BLOCK_ONLY_HIGH",
                    ),
                    types.SafetySetting(
                        category="HARM_CATEGORY_SEXUALLY_EXPLICIT",
                        threshold="BLOCK_ONLY_HIGH",
                    ),
                    types.SafetySetting(
                        category="HARM_CATEGORY_DANGEROUS_CONTENT",
                        threshold="BLOCK_ONLY_HIGH",
                    ),
                ],
            )

            try:
                # 新版 google-genai 通过统一 Client 暴露模型服务。上下文管理器
                # 会在请求结束后关闭底层 HTTP 连接，避免频繁生成时积累连接资源。
                with genai.Client(
                    api_key=api_key,
                    http_options=http_options,
                ) as client:
                    response = client.models.generate_content(
                        model=model_name,
                        contents=prompt,
                        config=generation_config,
                    )
                generated_text = response.text
            except (AttributeError, IndexError, ValueError) as e:
                logger.warning(f"gemini returned invalid response content: {str(e)}")
                raise ValueError(f"[{llm_provider}] returned invalid response content")

            return _normalize_text_response(generated_text, llm_provider)

        if adapter == "cloudflare_ai_gateway":
            account_id = extra_values["account_id"]
            gateway_id = extra_values["gateway_id"]
            # Cloudflare 当前推荐的 AI Gateway REST API 兼容 OpenAI SDK。
            # Account ID 用于构造统一端点，Gateway ID 通过请求头选择；这里
            # 不再调用 Workers AI 的 /ai/run/{model} 专用接口。
            client = OpenAI(
                api_key=api_key,
                base_url=(
                    f"https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1"
                ),
                default_headers={"cf-aig-gateway-id": gateway_id},
            )
            response = client.chat.completions.create(
                model=model_name,
                messages=[{"role": "user", "content": prompt}],
            )
            return _extract_chat_completion_text(response, llm_provider)

        if adapter == "litellm":
            import litellm

            if not model_name:
                raise ValueError(
                    f"{llm_provider}: model_name is not set, please set it in the config.toml file."
                )

            response = litellm.completion(
                model=model_name,
                messages=[{"role": "user", "content": prompt}],
                drop_params=True,
            )

            if not response:
                raise ValueError(f"[{llm_provider}] returned empty response")
            if not getattr(response, "choices", None):
                raise ValueError(f"[{llm_provider}] returned empty response")

            return _extract_chat_completion_text(response, llm_provider)

        if adapter == "azure":
            # Azure OpenAI SDK 使用 `azure_endpoint` 和 `api_version` 生成专用请求地址，
            # 不能继续复用下面普通 OpenAI-compatible 的 `base_url` 初始化逻辑。
            # 这里在 Azure 分支内完成请求并立即返回，避免客户端被后续 fallback
            # 覆盖，导致用户配置的 Azure 凭证通过校验但实际请求没有被使用。
            logger.info(f"requesting azure chat completion, model: {model_name}")
            client = AzureOpenAI(
                api_key=api_key,
                api_version=api_version,
                azure_endpoint=base_url,
            )
            response = client.chat.completions.create(
                model=model_name, messages=[{"role": "user", "content": prompt}]
            )
            if response:
                if isinstance(response, ChatCompletion):
                    return _extract_chat_completion_text(response, llm_provider)
                else:
                    raise Exception(
                        f'[{llm_provider}] returned an invalid response: "{response}", please check your network '
                        f"connection and try again."
                    )
            else:
                raise Exception(
                    f"[{llm_provider}] returned an empty response, please check your network connection and try again."
                )

        if adapter == "modelscope":
            content = ""
            client = OpenAI(
                api_key=api_key,
                base_url=base_url,
            )
            response = client.chat.completions.create(
                model=model_name,
                messages=[{"role": "user", "content": prompt}],
                extra_body={"enable_thinking": False},
                stream=True,
            )
            if response:
                for chunk in response:
                    if not chunk.choices:
                        continue
                    delta = chunk.choices[0].delta
                    if delta and delta.content:
                        content += delta.content

                if not content.strip():
                    raise ValueError("Empty content in stream response")

                return _normalize_text_response(content, llm_provider)
            else:
                raise Exception(f"[{llm_provider}] returned an empty response")

        client = OpenAI(
            api_key=api_key,
            base_url=base_url,
        )

        response = client.chat.completions.create(
            model=model_name, messages=[{"role": "user", "content": prompt}]
        )
        if response:
            if isinstance(response, ChatCompletion):
                return _extract_chat_completion_text(response, llm_provider)
            else:
                raise Exception(
                    f'[{llm_provider}] returned an invalid response: "{response}", please check your network '
                    f"connection and try again."
                )
        else:
            raise Exception(
                f"[{llm_provider}] returned an empty response, please check your network connection and try again."
            )

    except Exception:
        raise


def _is_transient_llm_error(error) -> bool:
    """Detect transport-level and quota/throttle failures worth retrying.

    Gemini and other providers surface temporary overload as HTTP 429/5xx or as
    a plain-text message inside the exception. Anything else (bad key, invalid
    model, network refuse) is permanent and must fail immediately.
    """
    status = getattr(error, "status_code", None)
    if status is None:
        status = getattr(error, "code", None)
    if isinstance(status, str):
        status = status.split(" ", 1)[0]
    try:
        code = int(status)
    except (TypeError, ValueError):
        code = 0
    text = str(error or "").lower()
    if code and code in _transient_status_codes:
        if code == 429 and any(marker in text for marker in _permanent_429_markers):
            return False
        return True
    return any(marker in text for marker in _transient_error_markers)


def _generate_response(prompt: str, app_config=None, *, retries: int | None = None) -> str:
    """Single-call wrapper that retries transient provider failures.

    Transient errors (429/5xx, quota/overload) are retried with capped
    exponential backoff and jitter. Permanent errors return immediately as the
    same "Error: ..." string as before, preserving the public contract.
    """
    attempts = _max_retries if retries is None else max(0, int(retries))
    for attempt in range(attempts):
        try:
            return _generate_response_once(prompt, app_config)
        except Exception as e:
            if not _is_transient_llm_error(e) or attempt >= attempts - 1:
                return f"Error: {_sanitize_error_message(e)}"
            delay = min(
                _transient_retry_seconds_cap,
                _transient_retry_seconds_baseline * (2 ** attempt),
            )
            delay *= 0.8 + 0.4 * _random.random()
            logger.warning(
                f"llm transient error on attempt {attempt + 1}/{attempts}, "
                f"retrying in {delay:.1f}s: {_sanitize_error_message(e)}"
            )
            _time.sleep(delay)
    return f"Error: llm request failed after {attempts} attempts"


def test_connection() -> tuple[bool, str, float]:
    """
    使用当前 Provider 配置发起一次最小请求，验证实际生成链路是否可用。

    连接测试直接复用 `_generate_response()`，因此会覆盖 API Key、Base URL、
    模型名称和 Provider 专用字段，但不会进入脚本生成的重试逻辑，也不会发送
    用户的视频主题或文案。返回值依次为成功状态、错误信息和请求耗时。
    """
    started_at = perf_counter()
    response = _generate_response(
        prompt="Reply with exactly: OK",
        retries=1,
    )
    elapsed = perf_counter() - started_at

    if not response:
        error_message = "LLM returned an empty response"
        logger.warning(f"llm connection test failed: {error_message}")
        return False, error_message, elapsed

    if response.startswith("Error:"):
        error_message = response.removeprefix("Error:").strip()
        logger.warning(f"llm connection test failed: {error_message}")
        return False, error_message, elapsed

    logger.info(f"llm connection test succeeded, elapsed: {elapsed:.2f}s")
    return True, "", elapsed


def _limit_script_text(text: str | None, max_length: int, field_name: str) -> str:
    value = (text or "").strip()
    if len(value) <= max_length:
        return value

    # API 层已经用 Pydantic 做长度校验；这里继续兜底，是为了保护
    # WebUI 或内部服务直接调用 generate_script 时不会把超长提示词发送给模型，
    # 避免 token 成本异常和请求失败。
    logger.warning(
        f"{field_name} is too long and will be truncated to {max_length} characters."
    )
    return value[:max_length]


def _normalize_script_paragraph_number(paragraph_number: int | None) -> int:
    try:
        value = int(paragraph_number or MIN_SCRIPT_PARAGRAPH_NUMBER)
    except (TypeError, ValueError):
        value = MIN_SCRIPT_PARAGRAPH_NUMBER

    if value < MIN_SCRIPT_PARAGRAPH_NUMBER or value > MAX_SCRIPT_PARAGRAPH_NUMBER:
        # WebUI 和 API 都会限制范围；这里兜底处理内部调用，避免异常参数直接扩大
        # LLM 生成成本或生成空结果。
        logger.warning(
            f"script paragraph_number is out of range and will be clamped: {value}"
        )
        return max(MIN_SCRIPT_PARAGRAPH_NUMBER, min(value, MAX_SCRIPT_PARAGRAPH_NUMBER))

    return value


def build_script_prompt(
    video_subject: str,
    language: str = "",
    paragraph_number: int = 1,
    video_script_prompt: str = "",
    custom_system_prompt: str = "",
    channel: str = "",
) -> str:
    paragraph_number = _normalize_script_paragraph_number(paragraph_number)
    video_script_prompt = _limit_script_text(
        video_script_prompt, MAX_SCRIPT_PROMPT_LENGTH, "video_script_prompt"
    )
    custom_system_prompt = _limit_script_text(
        custom_system_prompt, MAX_SCRIPT_SYSTEM_PROMPT_LENGTH, "custom_system_prompt"
    )

    # 将“脚本生成规则”和“运行时上下文”分开拼接。这样高级用户即使覆盖默认
    # system prompt，也不会漏掉视频主题、语言、段落数这些每次生成都必须带上的参数。
    prompt = custom_system_prompt or DEFAULT_SCRIPT_SYSTEM_PROMPT

    # 赛道品牌指令。放在"运行时上下文"之前，让它和默认脚本规则同属一条指令链：
    # 高级用户覆盖 system prompt 时仍然会拿到（品牌句是频道资产，不能被覆盖掉）。
    from app.services.niche_brand import script_directives

    directives = script_directives(channel)
    if directives:
        prompt += "\n\n" + directives

    prompt += f"""

# Initialization:
- video subject: {video_subject}
- number of paragraphs: {paragraph_number}
""".rstrip()
    if language:
        prompt += f"\n- language: {language}"
    if video_script_prompt:
        prompt += f"""

# Additional User Requirements:
{video_script_prompt}
""".rstrip()

    return prompt


def generate_script(
    video_subject: str,
    language: str = "",
    paragraph_number: int = 1,
    video_script_prompt: str = "",
    custom_system_prompt: str = "",
    app_config=None,
    channel: str = "",
) -> str:
    paragraph_number = _normalize_script_paragraph_number(paragraph_number)
    video_script_prompt = _limit_script_text(
        video_script_prompt, MAX_SCRIPT_PROMPT_LENGTH, "video_script_prompt"
    )
    custom_system_prompt = _limit_script_text(
        custom_system_prompt, MAX_SCRIPT_SYSTEM_PROMPT_LENGTH, "custom_system_prompt"
    )
    prompt = build_script_prompt(
        video_subject=video_subject,
        language=language,
        paragraph_number=paragraph_number,
        video_script_prompt=video_script_prompt,
        custom_system_prompt=custom_system_prompt,
        channel=channel,
    )
    final_script = ""
    last_error = ""
    logger.info(
        "generating video script: "
        f"subject={video_subject}, paragraph_number={paragraph_number}, "
        f"has_custom_prompt={bool(video_script_prompt.strip())}, "
        f"has_custom_system_prompt={bool(custom_system_prompt.strip())}"
    )

    def format_response(response):
        # Clean the script
        # Remove asterisks, hashes
        response = response.replace("*", "")
        response = response.replace("#", "")

        # Remove markdown syntax.  Use non-greedy .*? so each bracket/paren
        # group is removed independently; the greedy form would eat all text
        # between the first opener and the last closer on the same line.
        response = re.sub(r"\[.*?\]", "", response)
        response = re.sub(r"\(.*?\)", "", response)

        # Split the script into paragraphs
        paragraphs = response.split("\n\n")

        # Select the specified number of paragraphs
        # selected_paragraphs = paragraphs[:paragraph_number]

        # Join the selected paragraphs into a single string
        return "\n\n".join(paragraphs)

    for i in range(_max_retries):
        try:
            if app_config is None:
                response = _generate_response(prompt=prompt)
            else:
                response = _generate_response(prompt=prompt, app_config=app_config)
            if response:
                final_script = format_response(response)
            else:
                logging.error("gpt returned an empty response")

            # Some upstream providers may return quota errors as plain text.
            if final_script and "当日额度已消耗完" in final_script:
                raise ValueError(final_script)

            # An "Error: ..." string means _generate_response already gave up
            # (or returned a permanent error). Remember it, then clear it so
            # the retry loop tries the next attempt instead of breaking.
            if final_script and final_script.startswith("Error: "):
                last_error = final_script
                final_script = ""

            if final_script:
                break
        except Exception as e:
            last_error = f"Error: {_sanitize_error_message(e)}"
            logger.error(f"failed to generate script: {e}")

        if i < _max_retries - 1:
            logger.warning(f"failed to generate video script, trying again... {i + 1}")
    if not final_script and last_error:
        final_script = last_error
    if "Error: " in final_script:
        logger.error(f"failed to generate video script: {final_script}")
    else:
        logger.success(f"completed: \n{final_script}")
    return final_script.strip()


def _strip_code_fence(text: str) -> str:
    """Strip a surrounding markdown code fence from an LLM response.

    Non-OpenAI providers (Claude, Gemini, …) frequently wrap JSON output in a
    ```json … ``` fence even when asked to return raw JSON. Removing it lets the
    first json.loads() succeed instead of falling through to the regex recovery
    path (and spuriously logging a warning). Mirrors the DOTALL handling already
    used in _parse_social_metadata().
    """
    t = (text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z0-9]*\s*", "", t)
        t = re.sub(r"\s*```$", "", t)
    return t.strip()


def generate_terms(
    video_subject: str,
    video_script: str,
    amount: int = 5,
    match_script_order: bool = False,
    app_config=None,
) -> List[str]:
    if match_script_order:
        goal = (
            f"Generate {amount} chronological stock-video search terms that follow "
            "the order of topics in the video script."
        )
        ordering_rule = (
            "6. keep the terms in the same order as the script narration; "
            "earlier terms must describe earlier visual moments."
        )
        # 有序关键词模式下，示例数量要和 amount 保持一致，避免模型被固定
        # 的 4 个示例误导，导致长文案只返回少量关键词，影响素材覆盖度。
        example_terms = [
            "opening visual topic",
            *[f"script visual topic {index}" for index in range(2, max(amount, 1))],
            "final visual topic",
        ]
        output_example = json.dumps(example_terms[:amount], ensure_ascii=False)
    else:
        goal = (
            f"Generate {amount} search terms for stock videos, depending on the "
            "subject of a video."
        )
        ordering_rule = ""
        output_example = (
            '["search term 1", "search term 2", "search term 3",'
            '"search term 4", "search term 5"]'
        )

    prompt = f"""
# Role: Video Search Terms Generator

## Goals:
{goal}

## Constrains:
1. the search terms are to be returned as a json-array of strings.
2. each search term should consist of 1-3 words, always add the main subject of the video.
3. you must only return the json-array of strings. you must not return anything else. you must not return the script.
4. the search terms must be related to the subject of the video.
5. reply with english search terms only.
{ordering_rule}

## Output Example:
{output_example}

## Context:
### Video Subject
{video_subject}

### Video Script
{video_script}

Please note that you must use English for generating video search terms; Chinese is not accepted.
""".strip()

    logger.info(f"subject: {video_subject}, match_script_order: {match_script_order}")

    search_terms = []
    response = ""
    for i in range(_max_retries):
        try:
            if app_config is None:
                response = _generate_response(prompt)
            else:
                response = _generate_response(prompt, app_config=app_config)
            if response.startswith("Error: "):
                # generate_terms 的公开返回类型是 List[str]。如果把 Provider 的
                # 错误文案原样返回，下游只做空值判断时会把非空字符串误认为成功，
                # 素材下载循环还会按字符遍历错误文案，产生无意义的外部请求。
                # 这里统一返回空列表，让任务编排层在真实故障位置立即结束任务。
                logger.error(f"failed to generate video terms: {response}")
                return []
            search_terms = json.loads(_strip_code_fence(response))
            if not isinstance(search_terms, list) or not all(
                isinstance(term, str) for term in search_terms
            ):
                logger.error("response is not a list of strings.")
                continue

        except Exception as e:
            logger.warning(f"failed to generate video terms: {str(e)}")
            if response:
                match = re.search(r"\[.*]", response, re.DOTALL)
                if match:
                    try:
                        search_terms = json.loads(match.group())
                    except Exception as e:
                        # 这里保留重试流程，但必须记录 LLM 返回的非标准 JSON，
                        # 否则后续排查搜索词为空时无法定位
                        # 是模型格式问题还是解析逻辑问题。
                        logger.warning(f"failed to generate video terms: {str(e)}")

        if search_terms and len(search_terms) > 0:
            break
        if i < _max_retries - 1:
            logger.warning(f"failed to generate video terms, trying again... {i + 1}")

    logger.success(f"completed: \n{search_terms}")
    return search_terms


# =============================================================================
# Long-form planning
#
# 长视频不能靠一次 LLM 调用出整份脚本：一次调用既写不出 3500 词（输出长度
# 会撞模型上限，被截断后是一篇结构崩坏的长文），也无法保证每一段都有独立的
# 信息增量——模型在长输出里会退化成换句话重复。
#
# 因此拆成两步：大纲一次成型（保证叙事连贯和钩子分布），再逐段写文案
# （每段独立，可单独重试而不推翻整条大纲）。这是 direktor 和 script2vid
# 都采用的形状，这里按本项目的重试与错误文案约定重写。
# =============================================================================

#: 大纲 JSON 的 schema 说明，直接进 prompt。给出显式 schema 而不是只说
#: "返回 JSON"，是因为各家模型对"返回 JSON"的理解差异极大，而给出字段名
#: 后解析成功率明显更高。
_OUTLINE_SCHEMA = """[
  {"title": "short section title", "intent": "one sentence: what this section must convince the viewer of"}
]"""


def _longform_context(
    video_subject: str,
    language: str,
    channel: str,
    video_script_prompt: str,
    custom_system_prompt: str,
) -> str:
    """长视频两步共用的上下文段，保证大纲和文案的风格约束完全一致。"""
    context = [f"### Video Subject\n{video_subject}"]
    if language:
        context.append(f"### Language\n{language}")
    if video_script_prompt.strip():
        context.append(
            "### Additional instructions\n" + video_script_prompt.strip()
        )
    if custom_system_prompt.strip():
        context.append(
            "### Additional system instructions\n" + custom_system_prompt.strip()
        )
    return "\n\n".join(context)


def generate_outline(
    video_subject: str,
    section_count: int,
    language: str = "",
    target_seconds: int = 0,
    channel: str = "",
    video_script_prompt: str = "",
    custom_system_prompt: str = "",
    app_config=None,
):
    """
    生成整条长视频的分段大纲，返回解析后的 JSON（列表或 dict）。

    与其他生成函数的约定不同：这里**不吞错误**。解析失败时抛异常，由调用方
    决定重试或降级——因为一份坏大纲会让后面每一段的文案都建立在错误结构上，
    静默返回空列表只会把问题推到更下游、更难排查的位置。
    """
    from app.services import niche_brand

    minutes = round(target_seconds / 60) if target_seconds else 0
    directives = niche_brand.script_directives(channel)

    prompt = f"""# Role: Long-form Video Outline Planner

## Goals:
Design a {section_count}-section outline for a long-form narrated video
{f"({minutes} minutes total)" if minutes else ""} about the subject below.
Each section becomes roughly one continuous stretch of narration with its own
stock footage.

## Constraints:
1. Return ONLY a JSON array matching this schema:
{_OUTLINE_SCHEMA}
2. Return exactly {section_count} entries. No more, no fewer.
3. Each "intent" must be one concrete claim the viewer gains in that section.
   "More information about the topic" is not an intent.
4. Sections must escalate. Section 1 earns attention, the middle sections pay
   it off with substance, the last section lands the payoff. Do not let a
   section restate an earlier one.
5. The hook and brand line belong to section 1 only. Never plan a "greeting",
   an "intro", or an "outro" section.
6. "title" is for internal planning only. It is never spoken or shown on screen.
7. Write "title" and "intent" in the same language as the narration.
{directives}

## Context:
{_longform_context(video_subject, language, channel, video_script_prompt, custom_system_prompt)}
""".strip()

    logger.info(
        f"generating long-form outline: subject={video_subject}, "
        f"sections={section_count}, target_seconds={target_seconds}"
    )

    response = _generate_response(prompt=prompt, app_config=app_config)
    if not response or response.startswith("Error: "):
        raise RuntimeError(
            f"failed to generate long-form outline: "
            f"{response.removeprefix('Error: ').strip() if response else 'empty response'}"
        )

    # 解析交给 longform.parse_json_block：它比 _strip_code_fence 多处理 think
    # 块和"JSON 前后带解释文字"两种情况，而长 prompt 下后者很常见。
    from app.services import longform

    payload = longform.parse_json_block(response)
    if payload is None:
        raise RuntimeError(
            "long-form outline response was not parseable JSON: "
            f"{response[:400]!r}"
        )
    return payload


def generate_section_text(
    section_title: str,
    section_intent: str,
    section_index: int,
    section_count: int,
    video_subject: str,
    max_chars: int,
    language: str = "",
    channel: str = "",
    neighbours: str = "",
    video_script_prompt: str = "",
    custom_system_prompt: str = "",
    app_config=None,
) -> str:
    """
    写出单段文案，返回清洗后的纯文本。

    单段失败会抛出异常而不是返回空串：空串会让这一段在最终脚本里消失，
    成片就会在某处突兀地跳过一段内容，而这比整条任务失败更难察觉。
    """
    from app.services import niche_brand

    directives = niche_brand.script_directives(channel)

    prompt = f"""# Role: Long-form Video Script Writer

## Goals:
Write section {section_index} of {section_count} of a long-form narrated video.

- Section title (internal only, never spoken): {section_title or "(unspecified)"}
- What this section must accomplish: {section_intent or "(unspecified)"}
{neighbours}

## Constraints:
1. Write continuous narration only. No headings, no markdown, no bullet points,
   no speaker labels, no stage directions.
2. Do NOT write the hook or the brand line unless this is section 1. Do not
   repeat the channel name in later sections.
3. Open on the substance of this section. Do not write transitions like
   "now let's look at", "moving on", "in this part".
4. Target {max_chars} characters. Every sentence must add something the viewer
   did not already have; if a sentence would not survive deletion, delete it.
5. End where this section's job is done. The next section will open its own door.
6. Write in the narration language. Numbers, names and quotes stay as written.
{directives}

## Context:
{_longform_context(video_subject, language, channel, video_script_prompt, custom_system_prompt)}
""".strip()

    logger.info(
        f"generating long-form section {section_index}/{section_count}: "
        f"title={section_title!r}"
    )

    last_error = ""
    for attempt in range(_max_retries):
        try:
            response = _generate_response(prompt=prompt, app_config=app_config)
            if response and response.startswith("Error: "):
                last_error = response.removeprefix("Error: ").strip()
                continue
            if not response:
                last_error = "empty response"
                continue
            cleaned = _format_longform_section(response)
            if not cleaned:
                last_error = "section text was empty after cleaning"
                continue
            if len(cleaned) < 40:
                last_error = f"section text was suspiciously short ({len(cleaned)} chars)"
                continue
            if attempt < _max_retries - 1:
                logger.warning(
                    f"section {section_index} rejected ({last_error}), retrying..."
                )
            return cleaned
        except Exception as exc:  # noqa: BLE001 - 单段失败只应重试本段
            last_error = _sanitize_error_message(exc)
            logger.warning(f"failed to generate long-form section: {last_error}")

    raise RuntimeError(
        f"failed to generate long-form section {section_index}: {last_error}"
    )


def _format_longform_section(response: str) -> str:
    """
    清掉模型在单段输出里混入的 markdown 残留。

    与 ``generate_script.format_response`` 的处理保持一致，但**不**做按空行
    再切段：分段拼接由 ``longform.render_sections_as_script`` 统一负责，这里
    再切一次会造成段内出现空行，字幕匹配时多出一段对不上脚本的空 cue。
    """
    text = response.strip()
    # 整段被包在代码围栏里是常见失败模式，先剥掉。
    text = _strip_code_fence(text)
    text = re.sub(r"^#{1,6}\s+.*$", "", text, flags=re.MULTILINE)
    text = text.replace("*", "").replace("#", "")
    text = re.sub(r"\[.*?\]", "", text)
    text = re.sub(r"\(.*?\)", "", text)
    # 压缩成单段文本：换行会让 TTS 停顿、字幕多出一行。
    text = re.sub(r"\s*\n\s*", " ", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def generate_section_terms(
    section_summaries: List[dict],
    video_subject: str,
    terms_per_section: int = 4,
    app_config=None,
) -> List[List[str]]:
    """
    为每一段生成各自的素材搜索关键词，返回 ``[[...], ...]``。

    ``section_summaries`` 是 ``[{"index", "title", "intent", "preview"}]`` 列表。
    一次调用产出全部段的关键词，而不是逐段调用：关键词之间需要保持叙事顺序
    和去重，一次调用才能看到全局；逐段调用既要 N 倍调用量，又无法跨段去重。

    解析失败返回空列表而不是抛异常：关键词缺失只会让画面质量下降，不会让
    成片丢失内容，交给上层的降级路径（按段标题兜底生成关键词）更合适。
    """
    if not section_summaries:
        return []

    roster = "\n".join(
        f"{entry.get('index')}. {entry.get('title') or ''} — "
        f"{entry.get('intent') or ''}\n"
        f"   narration starts: {(entry.get('preview') or '')[:240]}"
        for entry in section_summaries
    )

    prompt = f"""# Role: Stock Footage Search Planner

## Goals:
For EACH section listed below, write {terms_per_section} stock-footage search
terms that match what that specific section shows on screen.

## Constraints:
1. Return ONLY a JSON array of arrays of strings, one inner array per section,
   in the same order as the sections listed below.
2. Each search term is 1-3 words, in English only, regardless of narration language.
3. Terms must match the section's actual content. Section 5's footage must not
   be interchangeable with section 2's footage.
4. Within a section, terms must describe different shots. Repeating one term
   makes the downloader fetch the same clip repeatedly.
5. Reusing a term across sections is allowed only when the same visual subject
   genuinely recurs in the narration.
6. No numbering, no keys, no commentary. The array of arrays only.

## Output Example:
{json.dumps([["term a", "term b", "term c", "term d"]] * len(section_summaries), ensure_ascii=False)}

## Context:
### Video Subject
{video_subject}

### Sections
{roster}
""".strip()

    logger.info(
        f"generating section terms for {len(section_summaries)} sections "
        f"({terms_per_section} each)"
    )

    for attempt in range(_max_retries):
        try:
            response = _generate_response(prompt=prompt, app_config=app_config)
            if not response or response.startswith("Error: "):
                continue
            from app.services import section_materials

            buckets = section_materials.coerce_section_terms(
                longform_parse_json(response), len(section_summaries)
            )
            if any(bucket for bucket in buckets):
                return buckets
            logger.warning("section terms came back empty")
        except Exception as exc:  # noqa: BLE001 - 降级路径在上层
            logger.warning(f"failed to generate section terms: {_sanitize_error_message(exc)}")
        if attempt < _max_retries - 1:
            logger.warning(
                f"failed to generate section terms, trying again... {attempt + 1}"
            )

    return []


def longform_parse_json(response: str):
    """转调 ``longform.parse_json_block``，避免 llm.py 反向依赖具体解析实现。"""
    from app.services import longform

    return longform.parse_json_block(response)


# =============================================================================
# Social publishing metadata
#
# 根据视频主题和脚本生成发布到短视频平台时常用的 title、caption 和 hashtags。
# 这块能力只复用现有 LLM provider，不接入任何外部发布服务，也不影响视频生成主链路。
# =============================================================================

# 不同平台的文案长度和 hashtag 数量偏好不同。这里使用保守上限，避免模型返回
# 过长内容后调用方还需要二次裁剪。
SOCIAL_PLATFORMS = {
    "tiktok": {"title_max": 100, "caption_max": 2200, "hashtag_count": 5},
    "youtube_shorts": {"title_max": 100, "caption_max": 5000, "hashtag_count": 3},
    "instagram_reels": {"title_max": 125, "caption_max": 2200, "hashtag_count": 8},
    "facebook_reels": {"title_max": 125, "caption_max": 2200, "hashtag_count": 5},
}
DEFAULT_SOCIAL_PLATFORM = "tiktok"
DEFAULT_SOCIAL_LANGUAGE = "auto"
MAX_SOCIAL_SUBJECT_LENGTH = 500
MAX_SOCIAL_SCRIPT_LENGTH = 8000
MAX_SOCIAL_LANGUAGE_LENGTH = 64

SOCIAL_PLATFORM_LABELS = {
    "tiktok": "TikTok",
    "youtube_shorts": "YouTube Shorts",
    "instagram_reels": "Instagram Reels",
    "facebook_reels": "Facebook Reels",
}

# LLM 不可用时的通用兜底标签。这里故意不绑定某个国家或语种，保证 API
# 对中文、英文、越南语等不同场景都能返回可用结构。
DEFAULT_SOCIAL_HASHTAGS = [
    "#shorts",
    "#viral",
    "#trending",
    "#fyp",
    "#video",
    "#reels",
    "#creator",
    "#content",
]


def _resolve_social_platform(platform: str | None) -> str:
    value = (platform or "").strip().lower()
    return value if value in SOCIAL_PLATFORMS else DEFAULT_SOCIAL_PLATFORM


def _normalize_social_language(language: str | None) -> str:
    value = (language or DEFAULT_SOCIAL_LANGUAGE).strip()
    if len(value) > MAX_SOCIAL_LANGUAGE_LENGTH:
        logger.warning(
            "social metadata language is too long and will be truncated to "
            f"{MAX_SOCIAL_LANGUAGE_LENGTH} characters."
        )
        value = value[:MAX_SOCIAL_LANGUAGE_LENGTH]
    return value or DEFAULT_SOCIAL_LANGUAGE


def _limit_social_text(text: str | None, max_length: int, field_name: str) -> str:
    value = (text or "").strip()
    if len(value) <= max_length:
        return value

    # API 层会限制长度；这里继续兜底，是为了保护内部调用或未来 WebUI
    # 直接调用时不会把超长内容发送给模型，避免 token 成本异常。
    logger.warning(
        f"{field_name} is too long and will be truncated to {max_length} characters."
    )
    return value[:max_length]


def _social_language_instruction(language: str | None) -> str:
    language = _normalize_social_language(language)
    if language.lower() == DEFAULT_SOCIAL_LANGUAGE:
        return (
            "Use the same language as the video subject and script. If the subject "
            "and script use different languages, prefer the script language."
        )

    return f'Write "title" and "caption" in this language: {language}.'


def _clamp_text(text, max_length: int) -> str:
    value = ("" if text is None else str(text)).strip()
    if max_length and len(value) > max_length:
        return value[:max_length].rstrip()
    return value


def _normalize_hashtags(raw, count: int) -> List[str]:
    """
    将 LLM 返回的 hashtag 统一整理成 `#tag` 格式。

    LLM 可能返回字符串、数组、带空格的词组、重复标签或包含标点的内容。
    这里集中清洗，可以让接口响应结构稳定，也避免平台发布时出现空标签、
    重复标签或不符合常见格式的 hashtag。
    """
    if isinstance(raw, str):
        candidates = re.split(r"[\s,]+", raw)
    elif isinstance(raw, (list, tuple)):
        # 数组里的每一项视为一个完整标签，因此 "du lich" 会变成
        # "#dulich"，而不是拆成两个标签。
        candidates = [str(entry) for entry in raw]
    else:
        candidates = []

    seen = set()
    result: List[str] = []
    for item in candidates:
        tag = re.sub(r"[^\w]", "", item, flags=re.UNICODE)
        if not tag:
            continue
        key = tag.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(f"#{tag}")
        if count and len(result) >= count:
            break
    return result


def build_social_metadata_prompt(
    video_subject: str,
    video_script: str = "",
    language: str = DEFAULT_SOCIAL_LANGUAGE,
    platform: str = DEFAULT_SOCIAL_PLATFORM,
) -> str:
    video_subject = _limit_social_text(
        video_subject, MAX_SOCIAL_SUBJECT_LENGTH, "video_subject"
    )
    video_script = _limit_social_text(
        video_script, MAX_SOCIAL_SCRIPT_LENGTH, "video_script"
    )
    platform = _resolve_social_platform(platform)
    spec = SOCIAL_PLATFORMS[platform]
    label = SOCIAL_PLATFORM_LABELS.get(platform, platform)
    language_instruction = _social_language_instruction(language)

    prompt = f"""
# Role: Short-Video Social Media Copywriter

## Goal
Write engaging publishing metadata for a short video that will be posted on {label}.

## Constraints
1. Respond ONLY with a single valid minified JSON object. No markdown, no code fences, no commentary.
2. The JSON must contain exactly these keys: "title", "caption", "hashtags".
3. "title": a specific hook grounded in the supplied script, at most {spec["title_max"]} characters. Do not invent claims or use generic hype.
4. "caption": a concise description based on the supplied script, at most {spec["caption_max"]} characters. Do not put hashtags inside the caption. Include a call to action only when supported by the script; do not add engagement bait.
5. "hashtags": a JSON array of exactly {spec["hashtag_count"]} strings. Each must start with "#", contain no spaces, and be relevant to the topic and to {label}.
6. {language_instruction}

## Output Example
{{"title":"...","caption":"...","hashtags":["#example","#video"]}}

## Context
### Video Subject
{video_subject}

### Video Script
{video_script}
""".strip()
    return prompt


def _parse_social_metadata(response: str, platform: str) -> dict:
    spec = SOCIAL_PLATFORMS[_resolve_social_platform(platform)]

    data = None
    try:
        data = json.loads(_strip_code_fence(response))
    except Exception:
        # 部分模型会在 JSON 外层包一段说明文字或 markdown fence。
        # API 调用方只需要稳定结构，所以这里尝试提取第一个 JSON object。
        match = re.search(r"\{.*\}", response or "", re.DOTALL)
        if match:
            data = json.loads(match.group())

    if not isinstance(data, dict):
        raise ValueError("social metadata response is not a JSON object")

    title = _clamp_text(data.get("title", ""), spec["title_max"])
    caption = _clamp_text(data.get("caption", ""), spec["caption_max"])
    hashtags = _normalize_hashtags(data.get("hashtags", []), spec["hashtag_count"])

    if not title and not caption:
        raise ValueError("social metadata response is missing both title and caption")

    return {"title": title, "caption": caption, "hashtags": hashtags}


def _fallback_social_metadata(
    video_subject: str, video_script: str, platform: str
) -> dict:
    spec = SOCIAL_PLATFORMS[_resolve_social_platform(platform)]
    subject = (video_subject or "").strip()
    script = (video_script or "").strip()

    title = subject
    if not title and script:
        # 没有主题时，用脚本第一句兜底生成 title，避免接口返回空标题。
        title = re.split(r"(?<=[.!?。！？])\s+", script)[0]

    return {
        "title": _clamp_text(title, spec["title_max"]),
        "caption": _clamp_text(script or subject, spec["caption_max"]),
        "hashtags": _normalize_hashtags(DEFAULT_SOCIAL_HASHTAGS, spec["hashtag_count"]),
    }


def generate_social_metadata(
    video_subject: str,
    video_script: str = "",
    language: str = DEFAULT_SOCIAL_LANGUAGE,
    platform: str = DEFAULT_SOCIAL_PLATFORM,
) -> dict:
    """
    生成短视频发布文案元数据。

    返回结构固定为 `{"title": str, "caption": str, "hashtags": List[str]}`。
    如果 LLM 不可用或返回格式异常，会降级为通用启发式结果，保证 API
    调用方始终拿到可展示、可发布前编辑的数据结构。
    """
    platform = _resolve_social_platform(platform)
    language = _normalize_social_language(language)
    video_subject = _limit_social_text(
        video_subject, MAX_SOCIAL_SUBJECT_LENGTH, "video_subject"
    )
    video_script = _limit_social_text(
        video_script, MAX_SOCIAL_SCRIPT_LENGTH, "video_script"
    )
    prompt = build_social_metadata_prompt(
        video_subject=video_subject,
        video_script=video_script,
        language=language,
        platform=platform,
    )
    logger.info(f"generating social metadata: platform={platform}, language={language}")

    response = ""
    for i in range(_max_retries):
        try:
            response = _generate_response(prompt)
            if isinstance(response, str) and "Error: " in response:
                logger.error(f"failed to generate social metadata: {response}")
                break
            metadata = _parse_social_metadata(response, platform)
            logger.success(f"completed: \n{metadata}")
            return metadata
        except Exception as e:
            logger.warning(f"failed to parse social metadata: {str(e)}")

        if i < _max_retries - 1:
            logger.warning(
                f"failed to generate social metadata, trying again... {i + 1}"
            )

    logger.warning("falling back to heuristic social metadata")
    return _fallback_social_metadata(video_subject, video_script, platform)


if __name__ == "__main__":
    video_subject = "生命的意义是什么"
    script = generate_script(
        video_subject=video_subject, language="zh-CN", paragraph_number=1
    )
    print("######################")
    print(script)
    search_terms = generate_terms(
        video_subject=video_subject, video_script=script, amount=5
    )
    print("######################")
    print(search_terms)
