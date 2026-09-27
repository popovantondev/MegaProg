import unittest

from ai_dev.plan_gui import LANGUAGE_LABELS, TEXT, _doctor_ready, chatgpt_plan_prompt


class PlanGuiTests(unittest.TestCase):
    def test_chatgpt_prompt_is_localized_and_explains_chat_file_access(self):
        phrases = {
            'ru': ('Ты не видишь файлы на моём компьютере', 'Цель:', 'чистый JSON'),
            'de': ('Du kannst meine lokalen Dateien nicht sehen', 'Ziel:', 'reines JSON'),
            'en': ('You cannot read my local project', 'Goal:', 'raw JSON'),
        }
        for language, expected in phrases.items():
            with self.subTest(language=language):
                prompt = chatgpt_plan_prompt(language, 'Demo objective')
                self.assertTrue(all(phrase in prompt for phrase in expected))
                self.assertIn('Demo objective', prompt)
                if language != 'en':
                    self.assertNotIn('You cannot read my local project', prompt)

    def test_every_gui_action_has_all_three_locales(self):
        keys = set(TEXT['en'])
        self.assertEqual(keys, set(TEXT['ru']))
        self.assertEqual(keys, set(TEXT['de']))
        for key in ('approve', 'run', 'resume', 'stop', 'doctor', 'handoff'):
            self.assertTrue(TEXT['ru'][key])
            self.assertTrue(TEXT['de'][key])
            self.assertTrue(TEXT['en'][key])

    def test_language_picker_shows_native_language_names(self):
        self.assertEqual(LANGUAGE_LABELS, {'ru': 'Русский', 'de': 'Deutsch', 'en': 'English'})

    def test_codex_check_gives_actionable_localized_setup_guidance(self):
        expected = {
            'ru': ('Установите Codex CLI', 'выполните codex', 'API-ключ не нужен'),
            'de': ('Installieren Sie die Codex CLI', 'starten Sie codex', 'API-Schlüssel ist nicht nötig'),
            'en': ('Install Codex CLI', 'run codex', 'API key is needed'),
        }
        for language, phrases in expected.items():
            with self.subTest(language=language):
                message = TEXT[language]['doctor_bad']
                self.assertTrue(all(phrase in message for phrase in phrases))
                self.assertIn('developers.openai.com/codex/cli', message)
                self.assertNotIn('see the log for details', message.lower())

    def test_doctor_readiness_is_parsed_without_treating_other_json_as_result(self):
        self.assertIs(_doctor_ready('startup output only'), None)
        self.assertIs(_doctor_ready('{"other": true}\n{"ready": false}'), False)
        self.assertIs(_doctor_ready('{"ready": true}'), True)


if __name__ == '__main__':
    unittest.main()
