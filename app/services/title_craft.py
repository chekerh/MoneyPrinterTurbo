"""标题工艺：把选题变成点得动的标题。

短视频标题的成败基本在前 3 个字。但"标题党"那套（``你绝对想不到``、
``震惊``、``99% 的人不知道``）既降完播率，也正是 humanizer 点名的廉价套路
——它承诺的东西和视频无关，观众点进来发现被骗，账号权重就掉了。

真正能转化的标题只有三样东西，而且都可以验证：

1. **具体的量。** "4 centimetres" 比 "a little" 强；"6 billion tonnes" 比
   "very heavy" 强。具体是可信度。
2. **一个真实的缺口。** 说出结果的一部分，让观众想知道为什么——而不是把
   结论全部藏起来。藏得太多像标题党，藏得刚好是悬念。
3. **一个能想象的画面。** 读者要能在脑子里看到那个东西，否则不会点。

反例是抽象概括："the psychology of money" 什么画面都没有。
所以这个模块提供：每条赛道的标题配方、从选题生成候选标题、以及
:func:`critique` —— 指出一个标题缺了哪一样。

注意与 :mod:`app.services.niche_brand` 的分工：那边管**视频内部**的结构
（钩子、品牌句、长度），这里管**外部包装**（标题、缩略图、描述）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

__all__ = [
    "TitleCritique",
    "TITLE_RECIPES",
    "recipe_for",
    "critique",
    "has_number",
    "has_gap",
    "is_concrete",
]

#: 只认真实数字：整数、小数、量级词。排除年份和无意义编号。
_NUMBER = re.compile(
    r"\b(\d+(?:[.,]\d+)?\s*(?:%|percent|cm|mm|m|km|kg|g|tonnes?|tons?|"
    r"dollars?|cents?|years?|days?|hours?|minutes?|seconds?|times|"
    r"billion|million|thousand|dozen|half))\b",
    re.IGNORECASE,
)
_BARE_DIGIT = re.compile(r"\b\d[\d,.]*\b")

#: 这些词表示"抽象概括"，是短视频标题最常见的死法。
_ABSTRACT = re.compile(
    r"\b(psychology|philosophy|understanding|exploring|delving into|"
    r"the science of|the art of|the nature of|a look at|overview)\b",
    re.IGNORECASE,
)

#: 廉价标题党：承诺和内容无关的词。用 humanizer 的标准判定——它们只是在
#: 喊，不是在说。
_CLICKSBAIT = re.compile(
    r"(you won'?t believe|you need to know|shocking|mind[- ]blown|"
    r"this changes everything|secret(s)? (the|your)|nobody (tells|talks)|"
    r"what nobody|99%|the truth about|is wrong with|you shouldn'?t)",
    re.IGNORECASE,
)

#: 能想象出画面的动词/名词。抽象选题靠它落地成画面。
_CONCRETE = re.compile(
    r"\b(bridge|rope|canyon|moon|venus|neutron|star|coin|dollar|inflation|"
    r"interest|bridge|interview|memory|brain|crowd|street|market|"
    r"silk|road|engineer|inch|spoon|teaspoon|dollar|bias|trick)\b",
    re.IGNORECASE,
)


def has_number(title: str) -> bool:
    """标题里有没有具体的量。"""
    return bool(_NUMBER.search(title or "")) or bool(_BARE_DIGIT.search(title or ""))


#: 数量级。单位必须同时覆盖缩写和拼写：标题里写 "4 centimeters" 很常见，
#: 只认 "4 cm" 会漏掉这类反直觉的小数量。
_MAGNITUDE = re.compile(
    r"\b(\d[\d,.]*)\s*"
    r"(billion|million|thousand|trillion|"
    r"centimet(?:er|re)s?|millimet(?:er|re)s?|cm|mm|"
    r"tonnes?|tons?|kilomet(?:er|re)s?|miles?)\b",
    re.IGNORECASE,
)


def _striking_figure(title: str) -> bool:
    """标题里的数字是否大到或小到需要解释。"""
    for value, unit in _MAGNITUDE.findall(title or ""):
        try:
            number = float(value.replace(",", ""))
        except ValueError:
            continue
        if unit.lower() in ("billion", "million", "thousand", "trillion"):
            return True
        # 厘米/毫米级的小数量同样反直觉：4 厘米小到让人怀疑自己听错了
        if unit.lower().startswith(("c", "mm", "millimet")) and number <= 100:
            return True
        if unit.lower() in ("tonnes", "tons") and number >= 1000:
            return True
    return False


def has_gap(title: str) -> bool:
    """
    有没有留下真实的缺口。

    缺口 = 标题说了结果的一部分，但没说完"为什么"。``Why`` / ``How``
    开头的问句天然带缺口；陈述句则看它有没有"反直觉"成分。
    """
    text = title or ""
    if re.match(r"^\s*(why|how|what|which|when)\b", text, re.IGNORECASE):
        return True
    if _striking_figure(text):
        # 一个大到或小到离谱的数字本身就是缺口：观众会想知道怎么可能。
        # "One teaspoon weighs 6 billion tonnes" 把结论说满了，
        # 但正因为那个数字离谱，它照样点得动。
        return True
    return bool(
        re.search(r"\b(but|yet|still|twice|half|longer than|more than|"
                  r"wrong|instead|without|despite|quietly|already|even)\b", text, re.IGNORECASE)
    )


def is_concrete(title: str) -> bool:
    """
    标题是否给出了一个可以想象的画面。

    判据是"不是抽象概括"——只有 :data:`_ABSTRACT` 那几个泛化词算失败。
    **不能**反过来要求命中某个具体名词表：那张表是有限的，遇到表外的
    具体名词（desert、drawer、canyon…）就会误判，把好标题判成抽象。
    所以 :data:`_CONCRETE` 只作为加分信号，见 :func:`critique`。
    """
    return not _ABSTRACT.search(title or "")


@dataclass(frozen=True)
class TitleCritique:
    """对一个标题的判断。``score`` 0-100，缺哪样就明说缺什么。"""

    title: str
    has_number: bool
    has_gap: bool
    is_concrete: bool
    is_clicksbait: bool
    score: int
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_strong(self) -> bool:
        return self.score >= 70 and not self.is_clicksbait


#: 每个赛道的配方：这类选题最缺什么、先补什么。
TITLE_RECIPES: dict[str, str] = {
    "history-mysteries": (
        "Lead with one concrete artefact or measurement from the story, then "
        "raise the question nobody has answered. A number beats a date. The "
        "viewer should be able to picture the object in the title."
    ),
    "mind-psychology": (
        "Name the behaviour the viewer already does, then say what is actually "
        "happening in their head when they do it. Avoid labelling it with a "
        "pop-psychology term as the whole title. Second person converts here."
    ),
    "money-finance": (
        "Start from a number the viewer has seen on a statement or a price tag, "
        "then show what it really costs or earns. Never predict, never promise. "
        "One idea per title."
    ),
    "space-science": (
        "Open on the impossible figure, then let the scale do the work. "
        "Comparisons to everyday objects beat adjectives. Keep the unit."
    ),
}

_GENERIC_RECIPE = (
    "Lead with the most concrete thing in the topic, name a number if one "
    "exists, and leave one honest question open."
)


def recipe_for(channel: object) -> str:
    key = str(channel or "").strip().lower().replace(" ", "-").replace("_", "-")
    return TITLE_RECIPES.get(key, _GENERIC_RECIPE)


def critique(title: str) -> TitleCritique:
    """给标题打分并指出缺什么。不做重写——重写要人来看上下文。"""
    text = str(title or "").strip()
    number = has_number(text)
    gap = has_gap(text)
    concrete = is_concrete(text)
    bait = bool(_CLICKSBAIT.search(text))

    notes: list[str] = []
    score = 100

    if not text:
        return TitleCritique(text, False, False, False, False, 0, ("标题为空",))

    if not concrete:
        score -= 30
        notes.append("太抽象：读者在标题里看不到画面，换成一个具体的东西")
    elif _CONCRETE.search(text):
        score += 6  # 命中具体名词：小幅加分，不作为门槛
        score = min(score, 100)
    if not number:
        score -= 20
        notes.append("没有具体的量：加一个数字、金额或单位，可信度会明显不同")
    if not gap:
        score -= 20
        notes.append("没有缺口：结果说太满，观众没有点开的理由")
    if bait:
        # 不是普通扣分，而是**封顶**：标题党是信任问题，不是文笔问题。
        # 允许它被"有数字/有缺口"这些优点补偿回来，等于鼓励两边都占。
        score = min(score - 40, 45)
        notes.append("标题党：承诺和内容无关，点进来的人会立刻划走")

    if len(text) > 70:
        score -= 8
        notes.append("偏长：移动端会被截断，控制在 60 个字符左右")

    return TitleCritique(
        title=text,
        has_number=number,
        has_gap=gap,
        is_concrete=concrete,
        is_clicksbait=bait,
        score=max(0, min(100, score)),
        notes=tuple(notes),
    )
