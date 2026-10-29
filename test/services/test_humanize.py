"""humanizer 强制执行层的判定表。

每条断言都对应 humanizer skill 里一条真实条目，样本取自这个仓库实际生成的
脚本（``outputs/*/videos/*.posting.json`` 的 caption、日志里的 script 段）以及
skill 自带的 before/after。检测器是正则，误报会花掉一次模型调用并可能改坏一句
正常口播，所以"正常人话不报警"和"AI 腔一定报警"同样重要。
"""

import unittest

from app.services import humanize
from app.services.humanize import (
    HUMANIZER_RULES,
    build_humanize_prompt,
    find_tells,
    humanize_script,
    needs_rewrite,
    normalize,
    score,
)


def keys(text):
    return [tell.key for tell in find_tells(text)]


class TestFindTells(unittest.TestCase):
    def test_plain_narration_has_no_tells(self):
        script = (
            "Roman concrete got stronger in seawater. The harbour walls pulled "
            "minerals out of the sea. Modern engineers still test the recipe."
        )
        self.assertEqual(find_tells(script), [])

    def test_not_x_but_y_single_sentence(self):
        tells = find_tells(
            "It's not just about the money, it's about who decides where it goes."
        )
        self.assertIn("not-x-but-y", [t.key for t in tells])

    def test_not_only_x_but_y(self):
        tells = find_tells("The wall was not only a defence but a statement of power.")
        self.assertIn("not-x-but-y", [t.key for t in tells])

    def test_not_x_but_y_split_across_sentences(self):
        tells = find_tells(
            "This does not mean the plan failed. It means nobody could read it."
        )
        self.assertIn("not-x-but-y", [t.key for t in tells])

    def test_a_plain_negation_is_not_a_tell(self):
        """“没有水就没有砖”不是 not-X-but-Y，不能报警。"""
        self.assertNotIn(
            "not-x-but-y",
            keys("Without water there is no brick, and the kiln stays cold."),
        )

    def test_inflated_significance(self):
        tells = find_tells(
            "The library stands as a testament to the city's ambition and marks a "
            "pivotal moment in scholarship."
        )
        self.assertIn("inflated-significance", [t.key for t in tells])

    def test_sales_language(self):
        tells = find_tells(
            "Nestled in the heart of the valley, the town has breathtaking views "
            "and is a must-visit stop."
        )
        self.assertIn("sales-language", [t.key for t in tells])

    def test_shallow_ing_rider(self):
        tells = find_tells(
            "The city grew rich on the strait, reflecting its control of the trade route."
        )
        self.assertIn("ing-rider", [t.key for t in tells])

    def test_strong_ai_word(self):
        tells = find_tells("We delve into the tapestry of the old kingdom.")
        self.assertIn("ai-words-strong", [t.key for t in tells])

    def test_one_mild_ai_word_is_not_enough(self):
        """“navigate” 是人也会用的词，单次出现不该触发重写。"""
        tells = find_tells("Sailors had to navigate the narrows in the dark.")
        self.assertFalse(needs_rewrite(tells))

    def test_three_mild_ai_words_are_a_pattern(self):
        tells = find_tells(
            "Sailors had to navigate the narrows, and the landscape shaped the "
            "crucial timing that made the crossing valuable."
        )
        mild = [t for t in tells if t.key == "ai-words-mild"]
        self.assertTrue(mild)
        self.assertEqual(mild[0].weight, 2)

    def test_chatbot_residue_always_needs_a_rewrite(self):
        tells = find_tells(
            "Great question! The bridge opened in 1883. I hope this helps."
        )
        self.assertIn("chatbot-residue", [t.key for t in tells])
        self.assertTrue(needs_rewrite(tells))

    def test_knowledge_disclaimer(self):
        tells = find_tells(
            "While specific details about the founder are limited, the shop opened "
            "in the 1890s."
        )
        self.assertIn("knowledge-disclaimer", [t.key for t in tells])

    def test_staged_runup(self):
        tells = find_tells("When it comes to Roman roads, the engineering still lasts.")
        self.assertIn("staged-runup", [t.key for t in tells])

    def test_one_line_closer(self):
        tells = find_tells("The mine closed in 1962 and never reopened. Let that sink in.")
        self.assertIn("one-line-closer", [t.key for t in tells])

    def test_a_single_dash_is_not_enough(self):
        tells = find_tells("The road ran north — four hundred miles of it.")
        self.assertFalse(needs_rewrite(tells))

    def test_two_dashes_need_a_rewrite(self):
        tells = find_tells(
            "The road — four hundred miles of it — ran north before it stopped."
        )
        self.assertIn("dashes", [t.key for t in tells])
        self.assertTrue(needs_rewrite(tells))

    def test_dash_count_survives_in_the_evidence(self):
        tells = find_tells("A — B. C — D.")
        dash = next(t for t in tells if t.key == "dashes")
        self.assertEqual(dash.count, 2)

    def test_curly_quotes_are_detected(self):
        tells = find_tells("The sign said “closed” for forty years.")
        self.assertIn("curly-quotes", [t.key for t in tells])

    def test_repeated_openings(self):
        tells = find_tells(
            "Constantine moved the capital. Constantine rebuilt the walls. "
            "Constantine changed the coin."
        )
        self.assertIn("repeated-openings", [t.key for t in tells])

    def test_pronoun_openings_are_not_a_tell(self):
        tells = find_tells("She ran. She hid. She waited until the soldiers left.")
        self.assertNotIn("repeated-openings", [t.key for t in tells])

    def test_markdown_markup(self):
        tells = find_tells("**Hook:** the ship sank in 1628.")
        self.assertIn("markup", [t.key for t in tells])

    def test_tells_come_back_in_reading_order(self):
        """按在脚本文本里的位置排序，不是按条目编号。"""
        tells = find_tells(
            "The town stands as a testament to trade. It's not just a port, it's a "
            "gateway."
        )
        self.assertEqual([t.key for t in tells][0], "inflated-significance")
        self.assertEqual([t.key for t in tells][-1], "not-x-but-y")

    def test_same_pattern_is_reported_once(self):
        tells = find_tells(
            "It's not just a wall, it's a promise. It's not just a road, it's a bridge."
        )
        self.assertEqual([t.key for t in tells].count("not-x-but-y"), 1)
        self.assertEqual(next(t for t in tells if t.key == "not-x-but-y").count, 2)


class TestNormalizeAndScore(unittest.TestCase):
    def test_normalize_fixes_curly_quotes_for_free(self):
        self.assertEqual(normalize("He said \u201cno\u201d."), 'He said "no".')

    def test_score_caps_repeats_so_one_bad_word_cannot_dominate(self):
        tell = humanize.Tell(
            key="ai-words-strong",
            section="12",
            name="Overused AI words",
            evidence="delve",
            count=50,
            weight=2,
            fix="plain word",
        )
        self.assertEqual(score([tell]), 6)


class TestHumanizeScript(unittest.TestCase):
    def test_clean_script_is_returned_untouched_without_calling_the_model(self):
        calls = []
        script = "The kiln reached 900 degrees. The bricks came out hard."
        result = humanize_script(script, generate=lambda prompt: calls.append(prompt))
        self.assertEqual(result.script, script)
        self.assertFalse(result.rewrote)
        self.assertEqual(calls, [])

    def test_rewrite_is_accepted_when_it_removes_the_tells(self):
        rewritten = (
            "The harbour walls pulled minerals out of the seawater. The concrete "
            "got stronger every year it sat there."
        )
        result = humanize_script(
            "The harbour walls delved into the seawater — a testament to Roman "
            "engineering, reflecting their genius.",
            generate=lambda prompt: rewritten,
        )
        self.assertTrue(result.rewrote)
        self.assertEqual(result.script, rewritten)
        self.assertLess(score(result.after), score(result.before))

    def test_rewrite_is_rejected_when_it_is_not_better(self):
        original = "Great question! The bridge opened in 1883. I hope this helps."
        result = humanize_script(original, generate=lambda prompt: original)
        self.assertFalse(result.rewrote)
        self.assertEqual(result.script, original)

    def test_rewrite_is_rejected_when_the_model_errors(self):
        result = humanize_script(
            "Great question! The bridge opened in 1883.",
            generate=lambda prompt: "Error: rate limited",
        )
        self.assertFalse(result.rewrote)

    def test_rewrite_is_rejected_when_it_drops_half_the_script(self):
        """重写把 130 词压成 20 词，说明它删了事实，不能接受。"""
        original = " ".join(["Great question!"] + ["The wall held for centuries."] * 30)
        result = humanize_script(original, generate=lambda prompt: "The wall held.")
        self.assertFalse(result.rewrote)

    def test_generator_exception_keeps_the_original(self):
        def boom(prompt):
            raise RuntimeError("provider down")

        result = humanize_script("Great question! It opened in 1883.", generate=boom)
        self.assertFalse(result.rewrote)
        self.assertTrue(result.script)

    def test_no_generator_means_deterministic_cleanup_only(self):
        result = humanize_script("He said \u201cstop\u201d. Great question!")
        self.assertFalse(result.rewrote)
        self.assertIn('"stop"', result.script)

    def test_preamble_and_wrapping_quotes_are_stripped(self):
        result = humanize_script(
            "Great question! The bridge opened in 1883.",
            generate=lambda prompt: (
                'Here is the rewritten script:\n\n"The bridge opened in 1883, '
                'eighteen years after the first proposal."'
            ),
        )
        self.assertTrue(result.rewrote)
        self.assertEqual(
            result.script, "The bridge opened in 1883, eighteen years after the first proposal."
        )


class TestPrompt(unittest.TestCase):
    def test_prompt_carries_each_evidence_and_fix(self):
        script = "The town stands as a testament to trade."
        tells = find_tells(script)
        prompt = build_humanize_prompt(script, tells)
        self.assertIn("stands as a testament", prompt)
        self.assertIn("keep every fact", prompt.lower())
        self.assertIn("do not add", prompt.lower())
        self.assertIn(script, prompt)

    def test_rules_block_mentions_the_rules_the_detector_enforces(self):
        for phrase in (
            "not X but Y",
            "em dashes",
            "stands as a testament",
            "sales language",
            "chatbot residue",
            "knowledge-limit",
        ):
            self.assertIn(phrase.lower(), HUMANIZER_RULES.lower())


if __name__ == "__main__":
    unittest.main()
