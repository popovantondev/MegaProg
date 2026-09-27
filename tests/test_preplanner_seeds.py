import subprocess
import tempfile
import unittest
import contextlib
import io
import json
from pathlib import Path
from unittest.mock import patch

from ai_dev import evidence_packet, planning_pipeline, supervisor
from ai_dev.storage import read


class PreplannerSeedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        subprocess.check_call(['git', 'init', '-q'], cwd=self.root)
        subprocess.check_call(['git', 'config', 'user.email', 'test@example.invalid'], cwd=self.root)
        subprocess.check_call(['git', 'config', 'user.name', 'Test'], cwd=self.root)
        paths = [
            'Sources/TranscriptionCore/AccurateTranscriptionRunner.swift',
            'Sources/TranscriptionCore/LocalTranscriptionRunner.swift',
            'Sources/TranscriptionCore/TranscriptionQuality.swift',
            'Sources/TranscriptionCore/ChunkedAudio.swift',
            'Sources/DeutschTranscriptionApp/AppModel.swift',
            'Sources/DeutschTranscriptionApp/ContentView.swift',
            'Tests/TranscriptionCoreTests/main.swift',
            'Scripts/check.sh', 'Scripts/test.sh', 'Scripts/build.sh',
            'Sources/DeutschTranscriptionApp/Resources/de.lproj/Localizable.strings',
            'Sources/DeutschTranscriptionApp/Resources/en.lproj/Localizable.strings',
            'Sources/DeutschTranscriptionApp/Resources/ru.lproj/Localizable.strings',
        ]
        for path in paths:
            target = self.root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text('"seed" = "transcription localization";\n', encoding='utf-8')
        config = self.root / '.ai-dev' / 'config.json'
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(json.dumps(supervisor.DEFAULT), encoding='utf-8')
        subprocess.check_call(['git', 'add', '.'], cwd=self.root)
        subprocess.check_call(['git', 'commit', '-qm', 'fixture'], cwd=self.root)
        self.paths = paths

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def seed(path, priority='required'):
        return {'kind': 'path', 'query': path, 'priority': priority, 'reason': 'fixture evidence'}

    def test_thirteen_seeds_including_strings_are_projected_without_model(self):
        rows = [self.seed(path) for path in self.paths]
        packet = evidence_packet.build_packet(self.root, goal='Inspect transcription and localization',
                                               initial_evidence_seeds=rows)
        self.assertEqual(len(packet['scope']['seeded_fragments']), 13)
        self.assertEqual(packet['budget']['model_turns_used_to_build'], 0)
        self.assertTrue(all(row['selection_reason'] == 'INITIAL_EVIDENCE_SEED'
                            for row in packet['scope']['seeded_fragments']))
        gate = {'status': 'READY', 'reasons': [], 'checks': {}}
        _, _, view = planning_pipeline.render_planner_input(packet, gate)
        self.assertEqual(len(view['scope']['seeded_fragments']), 13)

    def test_missing_required_exact_path_fails_with_stable_code(self):
        with self.assertRaises(evidence_packet.EvidenceSeedError) as caught:
            evidence_packet.build_packet(self.root, goal='Inspect source',
                initial_evidence_seeds=[self.seed('Sources/TranscriptionCore/Missing.swift')])
        self.assertEqual(caught.exception.code, 'PREPLANNER_EVIDENCE_SEED_MISSING')

    def test_invalid_shell_syntax_has_stable_code(self):
        with self.assertRaises(evidence_packet.EvidenceSeedError) as caught:
            evidence_packet.validate_initial_evidence_seeds([self.seed('Sources/$(touch bad).swift')])
        self.assertEqual(caught.exception.code, 'PREPLANNER_EVIDENCE_SEED_INVALID')
        self.assertEqual(caught.exception.as_dict()['rejected_seed_count'], 1)

    def test_required_seeds_that_cannot_fit_fail_without_silent_dropping(self):
        rows = [self.seed(path) for path in self.paths]
        with self.assertRaises(evidence_packet.EvidenceSeedError) as caught:
            evidence_packet.build_packet(self.root, goal='Inspect transcription', budget=4096,
                                         initial_evidence_seeds=rows)
        self.assertEqual(caught.exception.code, 'PREPLANNER_EVIDENCE_BUDGET_EXCEEDED')

    def test_optional_missing_path_is_visible_but_does_not_block(self):
        packet = evidence_packet.build_packet(self.root, goal='Inspect source',
            initial_evidence_seeds=[self.seed('Sources/TranscriptionCore/Missing.swift', 'optional')])
        row = packet['scope']['initial_evidence_seeds'][0]
        self.assertEqual(row['status'], 'MISSING')
        self.assertEqual(packet['budget']['model_turns_used_to_build'], 0)

    def test_task_persists_seeds_before_any_model_turn(self):
        seed = self.seed(self.paths[0])
        with patch.object(supervisor.codex, 'doctor', return_value={'ready': False}):
            result = supervisor.task(self.root, 'Inspect transcription', initial_evidence_seeds=[seed])
        self.assertEqual(result, 1)
        state = read(self.root / '.ai-dev/state.json')
        self.assertEqual(state['initial_evidence_seeds'], [seed])
        self.assertEqual(state['planning_telemetry']['planner_started_after_seed_validation'], False)
        self.assertEqual(state['planning_telemetry']['rejected_seed_count'], 0)

    def test_evidence_cli_flags_build_seed_packet_offline(self):
        from ai_dev.cli import main
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = main(['-C', str(self.root), 'evidence', 'build', '--goal', 'Inspect localization',
                           '--evidence-seed', self.paths[-1]])
        self.assertEqual(result, 0)
        self.assertIn('INITIAL_EVIDENCE_SEED', output.getvalue())
        self.assertIn('model turns: 0', output.getvalue())


if __name__ == '__main__':
    unittest.main()
