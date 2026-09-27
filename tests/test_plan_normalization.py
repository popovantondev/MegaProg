import json
import unittest

from ai_dev.planning import PlanParseError, parse_plan, parse_plan_detailed


def valid_plan(acceptance='Focused regression passes.'):
    return {'schema': 2, 'objective': 'Fix the requested behavior.', 'steps': [{
        'title': 'Fix and verify', 'worker_kind': 'normal',
        'files': ['src/app.py'], 'acceptance': acceptance}]}


class PlanNormalizationTests(unittest.TestCase):
    def test_valid_json_is_accepted_without_changes(self):
        plan, diag = parse_plan_detailed(json.dumps(valid_plan()))
        self.assertEqual(plan['objective'], 'Fix the requested behavior.')
        self.assertEqual(diag['parse_status'], 'ACCEPTED')
        self.assertEqual(diag['error_class'], 'NONE')
        self.assertFalse(diag['normalization_applied'])
        self.assertEqual(diag['normalization_actions'], [])
        self.assertTrue(diag['schema_valid'])
        self.assertTrue(diag['semantic_valid'])
        self.assertFalse(diag['retry_required'])

    def test_markdown_fence_is_removed_deterministically(self):
        raw = '```json\n' + json.dumps(valid_plan()) + '\n```'
        plan, diag = parse_plan_detailed(raw)
        self.assertEqual(plan['schema'], 2)
        self.assertEqual(diag['parse_status'], 'REPAIRED')
        self.assertEqual(diag['normalization_actions'], ['stripped_markdown_code_fence'])

    def test_one_object_can_be_extracted_from_surrounding_prose(self):
        raw = 'Planner result follows:\n' + json.dumps(valid_plan()) + '\nEnd of response.'
        plan, diag = parse_plan_detailed(raw)
        self.assertEqual(plan['steps'][0]['title'], 'Fix and verify')
        self.assertIn('extracted_single_json_object', diag['normalization_actions'])

    def test_bom_whitespace_and_trailing_commas_are_normalized(self):
        raw = '\ufeff \n' + json.dumps(valid_plan()).replace('"acceptance": "Focused regression passes."',
                                                              '"acceptance": "Focused regression passes.",')
        plan, diag = parse_plan_detailed(raw)
        self.assertTrue(plan['objective'])
        self.assertEqual(diag['normalization_actions'], [
            'removed_utf8_bom', 'trimmed_outer_whitespace', 'removed_trailing_comma'])

    def test_comma_like_text_inside_strings_is_not_rewritten(self):
        value = valid_plan('Keep literal text ,} exactly as written.')
        plan, diag = parse_plan_detailed(json.dumps(value))
        self.assertEqual(plan['steps'][0]['acceptance'], 'Keep literal text ,} exactly as written.')
        self.assertFalse(diag['normalization_applied'])

    def test_multiple_objects_fail_closed(self):
        raw = json.dumps(valid_plan()) + '\n' + json.dumps(valid_plan())
        with self.assertRaises(PlanParseError) as raised:
            parse_plan(raw)
        self.assertEqual(raised.exception.diagnostics['error_class'], 'FORMAT_ERROR')
        self.assertFalse(raised.exception.diagnostics['retry_required'])

    def test_object_nested_in_json_array_is_not_mistaken_for_plan(self):
        raw = '[' + json.dumps(valid_plan()) + ']'
        with self.assertRaises(PlanParseError) as raised:
            parse_plan_detailed(raw)
        self.assertEqual(raised.exception.diagnostics['error_class'], 'FORMAT_ERROR')

    def test_duplicate_fields_fail_closed_instead_of_taking_last_value(self):
        raw = '{"schema":2,"objective":"first","objective":"second","steps":[]}'
        with self.assertRaises(PlanParseError) as raised:
            parse_plan_detailed(raw)
        self.assertEqual(raised.exception.diagnostics['error_class'], 'FORMAT_ERROR')

    def test_unknown_fields_are_not_silently_discarded(self):
        value = valid_plan()
        value['architecture_note'] = 'do not lose this meaning'
        with self.assertRaises(PlanParseError) as raised:
            parse_plan_detailed(json.dumps(value))
        diag = raised.exception.diagnostics
        self.assertEqual(diag['error_class'], 'FORMAT_ERROR')
        self.assertIn('architecture_note', str(raised.exception))
        self.assertFalse(diag['retry_required'])

    def test_meaningful_but_overlong_acceptance_is_not_truncated_or_retried(self):
        value = valid_plan('A' * 1216)
        with self.assertRaises(PlanParseError) as raised:
            parse_plan_detailed(json.dumps(value))
        diag = raised.exception.diagnostics
        self.assertEqual(diag['error_class'], 'FORMAT_ERROR')
        self.assertFalse(diag['schema_valid'])
        self.assertIsNone(diag['semantic_valid'])
        self.assertFalse(diag['retry_required'])
        self.assertIn('небезопасно', str(raised.exception))


if __name__ == '__main__':
    unittest.main()
