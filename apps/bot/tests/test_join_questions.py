from __future__ import annotations

import re
import unittest

from misakabot.join_questions import GLOSSARY_QUESTIONS


class GlossaryQuestionTests(unittest.TestCase):
    def test_every_question_has_one_correct_and_four_distinct_short_options(self) -> None:
        self.assertTrue(GLOSSARY_QUESTIONS)
        self.assertEqual(
            len({question.prompt for question in GLOSSARY_QUESTIONS}),
            len(GLOSSARY_QUESTIONS),
        )
        for question in GLOSSARY_QUESTIONS:
            with self.subTest(prompt=question.prompt):
                self.assertEqual(len(question.options), 4)
                self.assertEqual(len(set(question.options)), 4)
                self.assertEqual(question.options.count(question.correct_option), 1)
                self.assertTrue(all(0 < len(option) <= 64 for option in question.options))


    def test_option_length_does_not_reveal_answer(self) -> None:
        # A script that always picks the longest or shortest option must not beat
        # random guessing (25%) by a meaningful margin.
        def success_rate(pick: object) -> float:
            total = 0.0
            for question in GLOSSARY_QUESTIONS:
                lengths = [len(option) for option in question.options]
                target = pick(lengths)  # type: ignore[operator]
                tied = [index for index, length in enumerate(lengths) if length == target]
                total += (question.options.index(question.correct_option) in tied) / len(tied)
            return total / len(GLOSSARY_QUESTIONS)

        self.assertLessEqual(success_rate(max), 0.3)
        self.assertLessEqual(success_rate(min), 0.3)

    def test_correct_option_does_not_repeat_the_quoted_term(self) -> None:
        for question in GLOSSARY_QUESTIONS:
            for term in re.findall(r"“([^”]+)”", question.prompt):
                with self.subTest(prompt=question.prompt, term=term):
                    # Repeating the term is only a giveaway when no distractor repeats it.
                    in_distractors = any(
                        term in option
                        for option in question.options
                        if option != question.correct_option
                    )
                    self.assertTrue(term not in question.correct_option or in_distractors)

    def test_parentheses_never_uniquely_identify_the_correct_option(self) -> None:
        for question in GLOSSARY_QUESTIONS:
            with self.subTest(prompt=question.prompt):
                marked = [option for option in question.options if "(" in option or "（" in option]
                self.assertFalse(marked == [question.correct_option])
