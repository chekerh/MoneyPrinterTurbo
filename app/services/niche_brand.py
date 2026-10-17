"""赛道品牌：每条视频的开场钩子 + 固定品牌句 + 该赛道专属的写法。

需求来自实际观察：同一个赛道里，每条视频的开头应该让人知道"这是同一条
频道"，同时第一句话就得把人按住。所以结构固定为三段：

    1. **钩子**（teaser）：一到两句，制造信息缺口。开场就是画面，不是寒暄。
    2. **品牌句**：每个赛道一句固定的话，作用是建立频道识别度。
    3. **正文**：实质内容。

顺序很关键：**钩子在品牌句之前**。品牌句如果放在最前面，就成了 humanizer
里说的"staged run-up"（铺垫），观众第一秒听到的却是套话。先给钩子再报
频道名，识别度和留存可以兼得。

品牌句本身必须**有信息量**。写成 "welcome to my channel" 就是纯套话；写成
"我们每天讲一个你会在饭桌上复述的故事"，它同时说明了频道做什么、为什么值得
听——这才是它能提升转化的地方。

赛道画像还可以覆盖：钩子的形状、这个赛道要避开的表达、目标时长。
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "NicheProfile",
    "NICHES",
    "DEFAULT_NICHE",
    "resolve_niche",
    "known_channels",
    "script_directives",
    "longform_preference",
]

DEFAULT_NICHE = "general"


@dataclass(frozen=True)
class NicheProfile:
    """一个赛道的品牌与写法约定。"""

    channel: str
    display_name: str
    #: 每条视频都会出现的固定品牌句。写死在这里，保证同一赛道完全一致。
    brand_line: str
    #: 钩子的形状。给模型的只是方向，不是要它照抄的句子。
    teaser_shapes: tuple[str, ...]
    #: 这个赛道特有的雷区。通用雷区在 prompt 里，这里只放赛道专属的。
    avoid: tuple[str, ...] = ()
    #: 目标口播秒数下限。脚本长度决定成片长度，宁可长也不要刚到线就断。
    min_seconds: int = 60
    #: 目标词数（英文口径）。约 2.5 词/秒。
    target_words: int = 220
    #: 这个赛道做**长视频**时的目标口播秒数。留空表示该赛道不做长视频，
    #: 显式指定 ``content_target_seconds`` 的任务不受此限制。
    #:
    #: 短视频频道和长视频频道的节奏要求完全不同：短视频要的是前 3 秒留人，
    #: 长视频要的是中途不流失。把两者混在同一个 ``target_words`` 里调整，
    #: 必然顾此失彼，所以长视频长度单独一个字段。
    longform_target_seconds: int = 0
    #: 长视频默认落在哪个档位。调用方未显式指定 content_length 时用它。
    longform_band: str = ""

    def brand_instruction(self) -> str:
        return (
            "After the hook, deliver this brand line word for word, in the same "
            f"language as the script:\n{self.display_name}: {self.brand_line}"
        )


#: 四个赛道的画像。品牌句刻意写成同一副骨架：频道名 + 每天做什么 + 为什么值得听。
#: 骨架一致是识别度的一部分，改一处要四处一起改。
NICHES: dict[str, NicheProfile] = {
    "history-mysteries": NicheProfile(
        channel="history-mysteries",
        display_name="History and Mysteries",
        brand_line=(
            "every day we look at the kind of story you will end up retelling "
            "at dinner"
        ),
        teaser_shapes=(
            "open on the consequence or the impossibility, then withhold the cause",
            "open on the artefact: what exists, and why nobody can explain it",
            "open on the ordinary person caught up in something enormous",
        ),
        avoid=(
            "war and empire as the only lens; skip battle trivia",
            "talking-head framing with no image to look at",
            "the mystery explained in the first ten seconds",
        ),
        min_seconds=75,
        target_words=280,
    ),
    "mind-psychology": NicheProfile(
        channel="mind-psychology",
        display_name="Mind and Psychology",
        brand_line=(
            "every day we unpack one experiment that explains something you "
            "already do without noticing"
        ),
        teaser_shapes=(
            "open on a behaviour the viewer recognises in themselves",
            "open on the uncomfortable result, then ask why it feels fair",
            "open on the everyday decision with a surprisingly bad track record",
        ),
        avoid=(
            "pop-psychology labels as the whole explanation",
            "diagnosing the viewer",
            "self-help advice at the end",
        ),
        min_seconds=70,
        target_words=260,
    ),
    "money-finance": NicheProfile(
        channel="money-finance",
        display_name="Money and Finance",
        brand_line=(
            "every day we make one money decision feel obvious instead of "
            "complicated"
        ),
        teaser_shapes=(
            "open on a number the viewer has seen but never questioned",
            "open on the moment a small choice quietly costs real money",
            "open on a rule everyone follows that is worse than the alternative",
        ),
        avoid=(
            "market predictions and timing calls",
            "get-rich framing and hustle advice",
            "lecturing the viewer's finances",
        ),
        min_seconds=70,
        target_words=255,
    ),
    "space-science": NicheProfile(
        channel="space-science",
        display_name="Space and Science",
        brand_line=(
            "every day we take one thing about the universe that sounds "
            "impossible and make it plain"
        ),
        teaser_shapes=(
            "open on the impossible measurement, then ask what it implies",
            "open with the scale: something too big or too far to picture",
            "open on the everyday assumption the science quietly overturns",
        ),
        avoid=(
            "treating metaphors as if they were the mechanism",
            "spacecraft trivia without a reason to care",
            "ending on a vague cosmic perspective",
        ),
        min_seconds=75,
        target_words=275,
    ),
}

_GENERAL = NicheProfile(
    channel=DEFAULT_NICHE,
    display_name="",
    brand_line="",
    teaser_shapes=("open on the most surprising concrete fact in the topic",),
    min_seconds=60,
    target_words=220,
)


def known_channels() -> tuple[str, ...]:
    return tuple(sorted(NICHES))


def resolve_niche(channel: object) -> NicheProfile:
    """
    按频道名取画像；认不出来就退回通用画像。

    故意不抛异常：频道名可能来自用户手输的 ``ln.json``，一个拼写错误不该
    让整条视频生成失败，只是拿不到品牌句而已。
    """
    key = str(channel or "").strip().lower().replace(" ", "-").replace("_", "-")
    return NICHES.get(key, _GENERAL)


def longform_preference(channel: object) -> tuple[str, int]:
    """
    取这个赛道长视频的 ``(档位, 目标秒数)``。

    赛道没配长视频目标时返回 ``("", 0)``，由调用方回退到请求里的
    ``content_length`` 或档位默认值。
    """
    profile = resolve_niche(channel)
    return profile.longform_band, profile.longform_target_seconds


def script_directives(channel: object) -> str:
    """
    生成给 LLM 的赛道指令段。通用画像返回空串，调用方可以无条件拼接。
    """
    profile = resolve_niche(channel)
    if profile.channel == DEFAULT_NICHE:
        return ""

    lines = [
        "## Channel voice and structure",
        "",
        "Every script follows this order. Do not reorder it.",
        "",
        f"1. **Hook ({profile.display_name}).** "
        + " ".join(profile.teaser_shapes)
        + " The first sentence must create a specific gap the viewer wants "
        "closed. No greeting, no channel name, no 'in this video' before it.",
        "",
        f"2. **Brand line.** {profile.brand_instruction()}",
        "",
        "3. **Body.** Deliver the substance. Every sentence must add something "
        "the viewer did not already have.",
    ]

    if profile.avoid:
        lines += [
            "",
            f"### Avoid in {profile.display_name}",
            *[f"- {item}" for item in profile.avoid],
        ]

    # 标题配方：外部包装的规则，和视频内部结构分开讲，避免模型把两者混在一起。
    from app.services.title_craft import recipe_for

    lines += [
        "",
        f"### Title for {profile.display_name}",
        recipe_for(profile.channel),
        "",
        "Openings work when they name one concrete thing (a number, an object, a "
        "measured amount) and leave one honest question open. Avoid the empty "
        "clickbait forms (you won't believe, shocking, 99% of people, the truth "
        "about): they promise something the video cannot deliver, and the viewer "
        "leaves as soon as it does.",
    ]

    lines += [
        "",
        f"### Length for {profile.display_name}",
        f"- Aim for about {profile.target_words} words, which is roughly "
        f"{profile.min_seconds} seconds of narration.",
        "- If the topic genuinely needs more room to land, go longer. Do not "
        "pad to hit a number.",
        "- A short script that says one thing well beats a long one that "
        "repeats itself.",
    ]
    return "\n".join(lines)
