"""标题工艺的判断规则。

这里最要紧的是**别把好标题判成坏标题**。第一版用一张具体名词表来判
"有没有画面"，结果 "desert"、"drawer"、"directions" 全被判成抽象——
一张有限的词表覆盖不了所有具体名词，误判比漏判更糟：好标题被扣分，
作者会照着改坏。

所以规则反过来：只有明确的抽象词（psychology、delving into…）才判失败；
具体名词表只用来加分。
"""

import unittest

from app.services.title_craft import (
    TITLE_RECIPES,
    critique,
    has_gap,
    has_number,
    is_concrete,
    recipe_for,
)


class TestNumberDetection(unittest.TestCase):
    def test_counts_units_and_quantities(self):
        for title, expected in (
            ("The Moon drifts 4 centimeters every year", True),
            ("One teaspoon weighs 6 billion tonnes", True),
            ("Why losing $1 hurts twice as much", True),
            ("Inca bridges crossed 500-foot canyons", True),
            ("Retell a story 3 times", True),
        ):
            with self.subTest(title=title):
                self.assertEqual(has_number(title), expected)

    def test_a_title_without_any_quantity_scores_worse(self):
        with_number = critique("One teaspoon of a neutron star weighs 6 billion tonnes")
        without = critique("A neutron star is very dense")
        self.assertGreater(with_number.score, without.score)


class TestGapDetection(unittest.TestCase):
    def test_questions_open_a_gap(self):
        for title in (
            "Why a day on Venus is longer than its year",
            "How books crossed the desert faster than any ship",
        ):
            with self.subTest(title=title):
                self.assertTrue(has_gap(title))

    def test_counterintuitive_results_open_a_gap(self):
        self.assertTrue(has_gap("Your brain picks the wrong option first"))
        self.assertTrue(has_gap("Why your memory edits itself when you retell a story"))

    def test_a_flat_statement_has_no_gap(self):
        self.assertFalse(
            has_gap("The Silk Road was a trade network across Asia"),
        )

    def test_a_striking_figure_is_a_gap_even_with_the_result_stated(self):
        """
        离谱的数字本身就是缺口。

        "6 billion tonnes" 把结论说满了，但因为那个数字反直觉，观众照样
        会想知道怎么可能——所以这种标题点得动，不该被判"没有缺口"。
        """
        self.assertTrue(
            has_gap("One teaspoon of a neutron star weighs 6 billion tonnes")
        )
        self.assertTrue(
            has_gap("The Moon drifts 4 centimeters away from Earth every year")
        )

    def test_a_title_stating_the_ending_has_no_gap(self):
        """把结局说完就没有悬念了，这正是原标题的毛病。"""
        self.assertFalse(
            has_gap("Retell a story 3 times and your memory will have changed it")
        )


class TestConcretenessHeuristic(unittest.TestCase):
    """
    具体名词表覆盖不到的词，不该被判成抽象。
    """

    def test_words_outside_the_noun_list_are_not_abstract(self):
        for title in (
            "How books crossed 6,000 miles of desert",
            "What inflation does to the cash in your drawer",
            "Why $500 saved pulls opposite to $500 borrowed",
        ):
            with self.subTest(title=title):
                self.assertTrue(
                    is_concrete(title),
                    "具体名词不该被判成抽象：词表覆盖不到不等于没有画面",
                )

    def test_genuinely_abstract_titles_are_caught(self):
        for title in (
            "A look at the psychology of money",
            "Delving into the philosophy of time",
            "Understanding the nature of growth",
        ):
            with self.subTest(title=title):
                self.assertFalse(is_concrete(title))

    def test_abstract_titles_score_below_concrete_ones(self):
        abstract = critique("A look at the psychology of money")
        concrete = critique("Why $500 saved pulls opposite to $500 borrowed")
        self.assertGreater(concrete.score, abstract.score)


class TestClicksbaitIsPenalised(unittest.TestCase):
    def test_empty_clickbait_is_detected_and_punished(self):
        for title in (
            "You won't believe what happened",
            "Shocking trick doctors hate",
            "99% of people get this wrong",
            "The secret your bank doesn't want you to know",
        ):
            with self.subTest(title=title):
                report = critique(title)
                self.assertTrue(report.is_clicksbait)
                self.assertLess(report.score, 50)

    def test_a_specific_title_beats_the_same_title_with_clickbait(self):
        honest = critique("Why your brain picks the wrong option first")
        baited = critique("The shocking truth about your brain")
        self.assertGreater(honest.score, baited.score)


class TestScoring(unittest.TestCase):
    def test_a_concrete_specific_open_title_scores_well(self):
        report = critique("One teaspoon of a neutron star weighs 6 billion tonnes")
        self.assertTrue(report.is_strong, report.notes)
        self.assertEqual(report.notes, ())

    def test_notes_explain_every_deduction(self):
        report = critique("A look at the psychology of money")
        self.assertTrue(report.notes, "扣分必须给出原因，否则作者不知道怎么改")

    def test_empty_title_scores_zero_without_raising(self):
        report = critique("")
        self.assertEqual(report.score, 0)
        self.assertTrue(report.notes)

    def test_score_stays_in_range(self):
        for title in (
            "",
            "a",
            "You won't believe this shocking 99% secret that changes everything",
            "The Moon drifts 4 centimeters away from Earth every year",
        ):
            with self.subTest(title=title):
                self.assertGreaterEqual(critique(title).score, 0)
                self.assertLessEqual(critique(title).score, 100)

    def test_overlong_titles_lose_points(self):
        short = critique("Why the Moon drifts away every year")
        long = critique(
            "Why the Moon drifts away from the Earth every single year and "
            "what that means for everybody"
        )
        self.assertGreater(short.score, long.score)


class TestRecipes(unittest.TestCase):
    def test_every_niche_has_a_recipe(self):
        for channel in (
            "history-mysteries",
            "mind-psychology",
            "money-finance",
            "space-science",
        ):
            with self.subTest(channel=channel):
                self.assertTrue(recipe_for(channel).strip())

    def test_unknown_channel_gets_the_generic_recipe(self):
        self.assertTrue(recipe_for("something-else").strip())

    def test_money_recipe_forbids_predictions(self):
        """金融赛道最容易滑向预测承诺，配方里必须明确禁止。"""
        self.assertIn("never predict", recipe_for("money-finance").lower())

    def test_recipe_keys_match_the_brand_module(self):
        from app.services.niche_brand import known_channels

        self.assertEqual(set(TITLE_RECIPES), set(known_channels()))


if __name__ == "__main__":
    unittest.main()
