"""humanizer 的强制执行层：先写对，再检出来，检出就重写一遍。

为什么要有这个模块而不是只往 prompt 里塞一句"别像 AI"：模型的自我评价不可
信。加了规则之后它依然会写出 "It's not just X, it's Y"、"a testament to"、
"nestled in the heart of"，而且它读自己的稿子时会认为这些句子很有力。所以这里
把 humanizer（``github.com/blader/humanizer``，本地 skill 目录里那份）里**可以
机械判定**的条目变成正则，检出后带着具体证据和具体改法再让模型改一次。

只收高精度的条目。误报的代价很实在：一次误报就是一次额外的 LLM 调用，还可能
把一句本来没毛病的口播改坏。所以强制三连（§6）、排比开头（§7）这类需要语感的
条目留在 prompt 里讲，不当成触发器。

同一个文本里同一类只报一次，附上出现次数和第一处证据——重写 prompt 需要的是
"哪里像 AI"，不是 20 条重复命中。

设计上与 :mod:`app.services.llm` 保持单向依赖：这里不 import llm，重写函数由
调用方传进来。否则 ``llm`` ↔ ``humanize`` 循环导入，而且测试还得把整个模型层
拖起来。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

__all__ = [
    "HUMANIZER_RULES",
    "HumanizeResult",
    "Tell",
    "build_humanize_prompt",
    "find_tells",
    "humanize_script",
    "normalize",
    "score",
    "needs_rewrite",
]

#: 写稿阶段就带上的规则。**这是唯一一份**：``llm.DEFAULT_SCRIPT_SYSTEM_PROMPT``
#: 用 f-string 嵌它，``llm.build_script_prompt`` 在自定义/赛道 system prompt 里
#: 缺这一段时补它。以前它只写在默认 prompt 里，而频道一旦提供
#: ``plans/<channel>/system_prompt.txt``（``history-mysteries`` 就是），整个默认
#: prompt 被替换，humanizer 规则跟着一起消失——正是那个频道写出最多 AI 腔。
#:
#: 只讲能在口播稿里判定的条目。标题、加粗、花引号这类排版问题对纯文本稿没意义，
#: 写进来只会让 prompt 变长、让模型分心。
HUMANIZER_RULES = r"""## Banned AI patterns (humanizer):
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
- no staged run-up ("in a world where", "when it comes to", "it's no secret
  that", "here's the thing", "picture this", "at the end of the day"). start
  at the hook.
- no "key role" / "crucial moment" / "lasting legacy" / "a step in the right
  direction". state what happened; the event is the significance.
- no em dash used twice as a connector. one dash is noise, two is a habit.
- no AI vocabulary outside the list above: tapestry, myriad, plethora, holistic,
  paradigm, synergy, cutting-edge, game-changer, leverage, seamless, intricate,
  meticulous, crucial, embark, resonate, elevate, harness, interplay.
- no "knowledge-limit" residue in a script: no "as of my last update", no "based
  on available information", no "it is believed that". if it is not known, leave
  it out."""


# ---------------------------------------------------------------------------
# 检测表
# ---------------------------------------------------------------------------

#: §12 模型高频词。分成"几乎只出现在模型文里"和"人也会用，但模型用得过密"两档，
#: 后一档单次命中只算轻症。
_AI_WORDS_STRONG = re.compile(
    r"\b(?:tapestry|testament|delve[sd]?|deep dive|meticulous(?:ly)?|pivotal|"
    r"myriad|plethora|holistic|paradigm|synergy|cutting-edge|game-?changer|"
    r"indelible|underscore[sd]?|showcase[sd]?|bolster(?:s|ed|ing)?|"
    r"garner(?:s|ed|ing)?)\b",
    re.IGNORECASE,
)
_AI_WORDS_MILD = re.compile(
    r"\b(?:landscape|realm|navigate|leverage|robust|seamless|intricate|"
    r"intricacies|crucial|foster(?:s|ed|ing)?|nuanced|embark(?:s|ed|ing)?|"
    r"resonate[sd]?|elevate[sd]?|harness(?:es|ed|ing)?|interplay|valuable|"
    r"enhance[sd]?|enduring|promising|vibrant|profound)\b",
    re.IGNORECASE,
)

#: §13 拔高意义。这些短语在纪录片脚本里也常见，但用在这里一律是空话：
#: 它们把"事情发生了"写成"事情改变了世界"。
_INFLATED = re.compile(
    r"\b(?:stands? as|stood as|a testament to|pivotal moment|crucial moment|"
    r"plays? a (?:key|vital|critical) role|played a (?:key|vital|critical) role|"
    r"marking a (?:new|turning|pivotal)|underscores? (?:the|its)|"
    r"reflects? a broader|lasting legacy|indelible mark|setting the stage for|"
    r"evolving landscape|continues? to thrive|the future looks bright|"
    r"a step in the right direction|speaks volumes)\b",
    re.IGNORECASE,
)

#: §16 广告腔。
_SALES = re.compile(
    r"\b(?:nestled|in the heart of|breathtaking|must-visit|stunning|renowned|"
    r"boasts?|diverse array|groundbreaking|rich cultural heritage|"
    r"natural beauty|revolutionary|transform your|unlock the|elevate your)\b",
    re.IGNORECASE,
)

#: §1 "not X but Y" 的三种常见形状。收紧到明确的转折标记，避免把
#: "not a person, but a process" 之外的普通否定句也算进来。
_NOT_X_BUT_Y = (
    re.compile(
        r"\bnot\s+(?:just|only|merely|simply)\b[^.!?\n]{0,90}?\bbut\b", re.IGNORECASE
    ),
    re.compile(
        r"\b(?:it'?s|it is|it isn'?t|this is|this isn'?t|that'?s|that isn'?t)\s+not\s+"
        r"[^.!?\n]{1,70}?[,;:—–-]\s*(?:it'?s|it is|this is|that'?s)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:does not|doesn'?t|is not|isn'?t)\s+(?:mean|about)\b[^.!?\n]{0,90}\.\s*"
        r"(?:it|this|that)\s+(?:means|is)\b",
        re.IGNORECASE,
    ),
)

#: §15 悬空的 -ing 补充语。要求前置逗号 + 明确的空话动词，精度高。
_ING_RIDER = re.compile(
    r",\s*(?:highlighting|underscoring|emphasi[sz]ing|ensuring|reflecting|"
    r"symboli[sz]ing|contributing to|cultivating|fostering|encompassing|"
    r"showcasing|solidifying|cementing|reinforcing|marking)\b"
    r"[^.!?\n]{0,90}",
    re.IGNORECASE,
)

#: §4 铺垫。开场前先解释"我要开始了"。
_STAGED_RUNUP = re.compile(
    r"\b(?:in a world where|when it comes to|it'?s no secret that|"
    r"here'?s the thing|let'?s dive in|let'?s be clear|picture this|"
    r"imagine a world|in today'?s fast-paced|at the end of the day)\b",
    re.IGNORECASE,
)

#: §2 纯复述式收尾。
_CLOSER = re.compile(
    r"\b(?:let that sink in|read that again|think about it|"
    r"that'?s the real win|that'?s the whole point)\b",
    re.IGNORECASE,
)

#: §22 对话残留。硬伤：出现就是模型把自己的回话写进稿子里了。
_CHATBOT = re.compile(
    r"(?:\bas an ai\b|\bas a language model\b|\bi hope this helps\b|"
    r"\bof course!|\bcertainly!|\bgreat question\b|\byou'?re absolutely right\b|"
    r"\bwould you like me to\b|\bwant me to\b|\blet me know\b|"
    r"\bshould i continue\b|\bhere'?s an overview\b|\bhere is an overview\b|"
    r"\bhere'?s a breakdown\b|\bhere is a breakdown\b|\bbelow is the script\b|"
    r"\bi'?ve written\b)",
    re.IGNORECASE,
)

#: §23 知识边界免责声明。
_DISCLAIMER = re.compile(
    r"\b(?:as of my|my knowledge|training data|up to my last|"
    r"based on (?:the )?available information|not publicly available|"
    r"i don'?t have access|i cannot verify|it is believed that|"
    r"while specific details|in the provided sources|limited information)\b",
    re.IGNORECASE,
)

#: §8 破折号。作为通用连接词出现时是排版习惯，不是语气。
_DASH = re.compile(r"—|–|(?<=\s)--(?=\s)")

#: §21 花引号。纯文本稿里出现只可能是模型输出的残留，可以机械替换。
_CURLY = re.compile(r"[\u2018\u2019\u201c\u201d]")

#: §19/§24 排版残留。``format_response`` 会先删掉这些，但重写回来的文本可能
#: 又带上，所以重写后还要再检一次。
_MARKUP = re.compile(r"(\*\*|__|^#{1,6}\s)", re.MULTILINE)

#: §7 连续同开头。只抓三个以上，且排除代词开头——"He did X. He did Y." 是
#: 正常叙述，不是排比。
_SENTENCE = re.compile(r"[^.!?]+[.!?]")
_OPENING_STOPLIST = frozenset(
    {"i", "it", "he", "she", "they", "we", "you", "the", "a", "an", "this", "that"}
)


@dataclass(frozen=True)
class Tell:
    """一处机械可判定的 AI 写作痕迹。"""

    key: str
    section: str
    name: str
    evidence: str
    count: int
    weight: int
    fix: str

    @property
    def strong(self) -> bool:
        return self.weight >= 2


@dataclass
class HumanizeResult:
    script: str
    before: list[Tell] = field(default_factory=list)
    after: list[Tell] = field(default_factory=list)
    rewrote: bool = False

    @property
    def improved(self) -> bool:
        return score(self.after) < score(self.before)


#: 需要对文本做一次改写的最低分。单条轻症（一个破折号、一个 "navigate"）不值得
#: 花一次模型调用；两条轻症或一条重症才动手。
REWRITE_THRESHOLD = 3


def _clean_evidence(text: str, limit: int = 120) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _first(pattern: re.Pattern, text: str) -> re.Match | None:
    return pattern.search(text)


def _group(
    key: str,
    section: str,
    name: str,
    pattern: re.Pattern,
    text: str,
    weight: int,
    fix: str,
) -> Tell | None:
    matches = list(pattern.finditer(text))
    if not matches:
        return None
    return Tell(
        key=key,
        section=section,
        name=name,
        evidence=_clean_evidence(matches[0].group(0)),
        count=len(matches),
        weight=weight,
        fix=fix,
    )


def _repeated_openings(text: str) -> list[tuple[int, Tell]]:
    """连续三句以上同一个词开头（代词除外）。"""
    run_word = ""
    run_start = 0
    run_count = 0
    best: tuple[int, Tell] | None = None
    sentences = list(_SENTENCE.finditer(text))
    for index, match in enumerate(sentences):
        words = match.group(0).split()
        first = re.sub(r"[^a-z']", "", words[0].lower()) if words else ""
        if not first:
            continue
        if first == run_word:
            run_count += 1
        else:
            run_word, run_start, run_count = first, index, 1
        if run_count >= 3 and run_word not in _OPENING_STOPLIST and best is None:
            evidence = " / ".join(
                _clean_evidence(m.group(0).strip(), 60)
                for m in sentences[run_start : index + 1]
            )
            best = (
                sentences[run_start].start(),
                Tell(
                    key="repeated-openings",
                    section="7",
                    name="Repeated sentence openings",
                    evidence=evidence,
                    count=run_count,
                    weight=1,
                    fix="merge the sentences, change the subject, or open with the action",
                ),
            )
    return [best] if best else []


def find_tells(text: str) -> list[Tell]:
    """按出现顺序返回所有可判定的痕迹，同类只报一次。"""
    if not text:
        return []
    found: list[tuple[int, Tell]] = []

    for pattern in _NOT_X_BUT_Y:
        match = _first(pattern, text)
        if match:
            found.append(
                (
                    match.start(),
                    Tell(
                        key="not-x-but-y",
                        section="1",
                        name="Not X but Y",
                        evidence=_clean_evidence(match.group(0)),
                        count=len(pattern.findall(text)),
                        weight=2,
                        fix=(
                            "state the point directly; keep the negative half only "
                            "if it corrects something the viewer believes"
                        ),
                    ),
                )
            )
            break

    simple = (
        ("inflated-significance", "13", "Inflated significance", _INFLATED, 2,
         "drop the significance and keep the fact"),
        ("sales-language", "16", "Sales language", _SALES, 2,
         "describe what the thing is instead of selling it"),
        ("ing-rider", "15", "Shallow -ing rider", _ING_RIDER, 2,
         "write the consequence as its own sentence with a subject"),
        ("staged-runup", "4", "Staged run-up", _STAGED_RUNUP, 2,
         "start at the hook; delete the announcement"),
        ("one-line-closer", "2", "One-line closer", _CLOSER, 2,
         "end on the last concrete fact"),
        ("chatbot-residue", "22", "Chatbot residue", _CHATBOT, 3,
         "remove the wrapper; keep only the content"),
        ("knowledge-disclaimer", "23", "Knowledge-limit disclaimer", _DISCLAIMER, 3,
         "remove the hedge; if the fact is unknown, leave it out"),
    )
    for key, section, name, pattern, weight, fix in simple:
        tell = _group(key, section, name, pattern, text, weight, fix)
        if tell:
            found.append((pattern.search(text).start(), tell))

    strong = _group(
        "ai-words-strong", "12", "Overused AI words", _AI_WORDS_STRONG, text, 2,
        "replace with the plain word a person would say",
    )
    if strong:
        found.append((_AI_WORDS_STRONG.search(text).start(), strong))

    mild = _group(
        "ai-words-mild", "12", "Overused AI words", _AI_WORDS_MILD, text, 1,
        "replace with the plain word a person would say",
    )
    if mild:
        # 单个轻症词是人话；成串出现才是模型腔。
        weight = 2 if mild.count >= 3 else 1
        found.append(
            (
                _AI_WORDS_MILD.search(text).start(),
                Tell(
                    key=mild.key,
                    section=mild.section,
                    name=mild.name,
                    evidence=mild.evidence,
                    count=mild.count,
                    weight=weight,
                    fix=mild.fix,
                ),
            )
        )

    dashes = list(_DASH.finditer(text))
    if dashes:
        found.append(
            (
                dashes[0].start(),
                Tell(
                    key="dashes",
                    section="8",
                    name="Dashes as a connector",
                    evidence=_clean_evidence(text[max(0, dashes[0].start() - 40) : dashes[0].end() + 40]),
                    count=len(dashes),
                    weight=2 if len(dashes) >= 2 else 1,
                    fix="replace each dash with a period, a comma or a colon",
                ),
            )
        )

    curly = list(_CURLY.finditer(text))
    if curly:
        found.append(
            (
                curly[0].start(),
                Tell(
                    key="curly-quotes",
                    section="21",
                    name="Curly quotation marks",
                    evidence=_clean_evidence(curly[0].group(0)),
                    count=len(curly),
                    weight=1,
                    fix="use straight quotes",
                ),
            )
        )

    markup = _first(_MARKUP, text)
    if markup:
        found.append(
            (
                markup.start(),
                Tell(
                    key="markup",
                    section="19",
                    name="Markdown formatting",
                    evidence=_clean_evidence(markup.group(0)),
                    count=len(_MARKUP.findall(text)),
                    weight=1,
                    fix="plain narration text only",
                ),
            )
        )

    for position, tell in _repeated_openings(text):
        found.append((position, tell))

    found.sort(key=lambda pair: pair[0])
    return [tell for _, tell in found]


def score(tells: list[Tell]) -> int:
    """痕迹总权重。同类多次命中只按前三次计入，避免一个烂词刷分。"""
    return sum(tell.weight * min(tell.count, 3) for tell in tells)


def needs_rewrite(tells: list[Tell]) -> bool:
    return score(tells) >= REWRITE_THRESHOLD


def normalize(text: str) -> str:
    """不需要模型就能修的部分：花引号、多余空白、首尾空白。"""
    if not text:
        return ""
    text = (
        text.replace("\u2018", "'")
        .replace("\u2019", "'")
        .replace("\u201c", '"')
        .replace("\u201d", '"')
    )
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


_REWRITTEN_HEADER = re.compile(
    r"^(?:here(?:'s| is) (?:the )?(?:rewritten|revised|edited) (?:script|version)[^\n]*|"
    r"rewritten (?:script|version)[^\n]*|sure[,!][^\n]*)\n+",
    re.IGNORECASE,
)


def _strip_wrapper(text: str) -> str:
    text = text.strip()
    text = _REWRITTEN_HEADER.sub("", text).strip()
    # 模型常把整段用引号包起来。
    for opener, closer in (('"', '"'), ("'", "'"), ("“", "”")):
        if len(text) > 2 and text.startswith(opener) and text.endswith(closer):
            text = text[len(opener) : -len(closer)].strip()
            break
    return text


def build_humanize_prompt(script: str, tells: list[Tell]) -> str:
    """重写指令。只给"改什么、怎么改"，不给"写得更好"——后者会让模型加事实。"""
    lines = [
        "# Task: remove AI writing patterns from a narration script",
        "",
        "You are editing, not writing. Keep every fact, number, name, date and quote",
        "that is already in the script. Do not add a fact, name, number, date, quote or",
        "claim that is not in it. If a sentence needs a detail it does not have, write a",
        "simpler sentence instead of inventing one.",
        "",
        "Keep the language, the hook in the first sentence, and the length within about",
        "15 percent of the original. Keep the order of events.",
        "",
        "## Patterns found",
        "",
    ]
    for tell in tells:
        times = f" (x{tell.count})" if tell.count > 1 else ""
        lines.append(f"- {tell.name}{times}: {tell.fix}")
        lines.append(f'  evidence: "{tell.evidence}"')
    lines += [
        "",
        "## Output",
        "",
        "Return only the rewritten narration. No preamble, no explanation, no headings,",
        "no markdown, no quotation marks around the whole thing.",
        "",
        "## Script",
        "",
        script.strip(),
        "",
    ]
    return "\n".join(lines)


def _word_count(text: str) -> int:
    return len(re.findall(r"[A-Za-z0-9']+", text))


def humanize_script(
    text: str,
    generate=None,
    rounds: int = 1,
    min_ratio: float = 0.55,
    max_ratio: float = 1.8,
) -> HumanizeResult:
    """检出痕迹并在需要时重写一次。

    ``generate`` 是 ``prompt -> str`` 的可调用对象（由 ``llm.generate_script``
    传入，避免循环导入）。没传就只做确定性清理，不冒险让别的模型乱改。
    重写结果比原文更差（痕迹更多、长度失控、为空）时保留原文。
    """
    cleaned = normalize(text)
    if not cleaned:
        return HumanizeResult(script="", before=[], after=[])

    before = find_tells(cleaned)
    result = HumanizeResult(script=cleaned, before=before, after=before)
    if generate is None or rounds <= 0 or not needs_rewrite(before):
        return result

    for _ in range(rounds):
        try:
            raw = generate(build_humanize_prompt(result.script, result.after))
        except Exception:  # 重写是增强，不是主路径，失败就不改
            return result
        candidate = normalize(_strip_wrapper(raw or ""))
        if not candidate or candidate.startswith("Error: "):
            return result
        original_words = _word_count(result.script)
        candidate_words = _word_count(candidate)
        if original_words and not (
            original_words * min_ratio <= candidate_words <= original_words * max_ratio
        ):
            return result
        candidate_tells = find_tells(candidate)
        if score(candidate_tells) >= score(result.after):
            return result
        result.script = candidate
        result.after = candidate_tells
        result.rewrote = True
        if not needs_rewrite(candidate_tells):
            break
    return result
