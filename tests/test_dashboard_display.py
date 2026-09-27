import json
import tempfile
import time
import unittest
from unittest.mock import patch
from pathlib import Path
from ai_dev.dashboard import dashboard, render, signature, heartbeat
from ai_dev.terminal_style import styled


class DashboardDisplayTests(unittest.TestCase):
    def test_monitor_explains_planner_worker_and_recovery_from_existing_state(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / '.ai-dev/state.json'
            path.parent.mkdir()
            planner = {'model': 'gpt-6-astra', 'reasoning': 'low', 'role': 'planner'}
            worker = {'model': 'gpt-5.6-terra', 'reasoning': 'medium', 'role': 'worker',
                      'reason': 'Тип задачи: hard', 'checkpoint': 'step-1'}
            state = {'status': 'RUNNING', 'routing_policy': 'plan-first-v1',
                     'planning_attempts': [{'decision': planner, 'codex': {'ok': True}}],
                     'plan': {'schema': 2, 'steps': [{}]}, 'decisions': [worker],
                     'active_decision': worker}
            path.write_text(json.dumps(state), encoding='utf-8')
            output = render(dashboard([folder]))
            self.assertIn('План составила: gpt-6-astra / low', output)
            self.assertIn('Модель: gpt-5.6-terra | режим: medium', output)
            self.assertIn('Почему эта модель: Тип задачи: hard', output)
            self.assertIn('После двух неудач: сильная модель', output)
            recovery = dict(planner, role='recovery', checkpoint='step-1',
                            reason='Повышение после двух неудач исполнителя.')
            state.update(active_decision=recovery, decisions=[worker, recovery], worker_failures=2)
            path.write_text(json.dumps(state), encoding='utf-8')
            output = render(dashboard([folder]))
            self.assertIn('Модель: gpt-6-astra | режим: low', output)
            self.assertIn('Смена исполнителя: gpt-5.6-terra / medium → gpt-6-astra / low', output)
            self.assertIn('Почему эта модель: Повышение после двух неудач', output)
            self.assertNotIn('После двух неудач: сильная модель', output)
            self.assertNotIn('\x1b', output)

    def test_overview_is_compact_and_does_not_duplicate_task_prompt(self):
        result = {'projects': [{'project': 'site', 'path': '/site', 'status': 'RUNNING',
                               'title': 'LONG PRIVATE TASK PROMPT'}], 'counts': {'RUNNING': 1}}
        text = render(result, overview=True)
        self.assertIn('ALL PROJECTS', text)
        self.assertIn('site', text)
        self.assertIn('живость пока не подтверждена', text)
        self.assertNotIn('LONG PRIVATE TASK PROMPT', text)

    def test_overview_lists_many_projects_in_one_line_each(self):
        projects = [{'project': 'project-%02d' % i, 'path': '/tmp/project-%02d' % i,
                     'status': 'COMPLETED', 'model': 'gpt-6-luna', 'reasoning': 'medium'}
                    for i in range(12)]
        projects[-1]['status'] = 'RUNNING'
        text = render({'projects': projects, 'counts': {'RUNNING': 1}}, overview=True)
        for i in range(12):
            self.assertEqual(text.count('[%s] project-%02d' %
                                        ('RUNNING' if i == 11 else 'COMPLETED', i)), 1)
        self.assertLessEqual(len(text.splitlines()), 22)
        self.assertTrue(text.index('[RUNNING] project-11') < text.index('══ ТЕКУЩАЯ ЗАДАЧА: project-11'))

    def test_views_label_scope_and_detail_only_claims_observed_work(self):
        result = {'projects': [{'project': 'site', 'path': '/site', 'status': 'RUNNING',
                                'model': 'gpt-5.6-luna', 'reasoning': 'low', 'attempts': 2,
                                'updated_seconds_ago': 75, 'verify': 'NOT_RUN',
                                'progress': {'stage': 'step-2', 'doing': 'ожидание ответа',
                                             'next_step': 'проверить журнал'}}],
                  'counts': {'RUNNING': 1}}
        detail = render(result)
        overview = render(result, overview=True)
        self.assertIn('THIS TASK', detail)
        self.assertIn('ALL PROJECTS', overview)
        self.assertIn('Наблюдаемый этап: step-2 | операция: ожидание ответа', detail)
        self.assertIn('Последний результат: NOT_RUN | следующая операция: проверить журнал', detail)
        self.assertIn('живость исполнителя не подтверждена', detail)

    def test_current_work_is_last_and_visually_distinct_in_both_monitors(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            entries = []
            for name, status, age in (('new-running', 'RUNNING', 2),
                                      ('older-running', 'RUNNING', 80),
                                      ('stopped', 'BLOCKED', 1)):
                project = root / name
                (project / '.ai-dev').mkdir(parents=True)
                (project / '.ai-dev/state.json').write_text(json.dumps({
                    'id': name, 'status': status, 'prompt': 'Fix ' + name,
                    'updated_at': time.time() - age,
                    'progress': {'next_step': 'Check results'}}), encoding='utf-8')
                entries.append(project)
            result = dashboard(entries)
            self.assertEqual([item['project'] for item in result['projects']],
                             ['stopped', 'older-running', 'new-running'])
            overview = render(result, overview=True)
            self.assertLess(overview.index('older-running'), overview.rindex('══ ТЕКУЩАЯ ЗАДАЧА: new-running ══'))
            self.assertNotIn('Fix new-running', overview)
            detail = render(dashboard([entries[0]]))
            self.assertLess(detail.index('  Папка:'), detail.rindex('══ ТЕКУЩАЯ ЗАДАЧА: new-running ══'))
            self.assertTrue(detail.rstrip().endswith('Дальше: Check results'))
            self.assertIn('Fix new-running', detail)
            with patch.dict('os.environ', {'MEGAPROG_THEME': 'dark'}):
                self.assertIn('\x1b[1;93m══ ТЕКУЩАЯ ЗАДАЧА:', styled(detail, enabled=True))
            self.assertNotIn('\x1b', styled(detail, enabled=False))

    def test_orchestration_failure_has_copyable_diagnostic(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            tasks = root / '.ai-dev/orchestration/tasks'
            tasks.mkdir(parents=True)
            (tasks / 'task.json').write_text(json.dumps({
                'task_id': 'lecture-test', 'status': 'BLOCKED', 'updated_at': 1,
                'verification': {'checks': [{'name': 'verify', 'exit_code': 1,
                                             'output_tail': 'PermissionError: test denied\n\x1b[2J'}]}
            }), encoding='utf-8')
            result = dashboard([root])
            output = render(result)
            self.assertEqual(result['counts'], {'BLOCKED': 1})
            self.assertIn('ERROR COPY START', output)
            self.assertIn('PermissionError', output)
            self.assertIn('lecture-test', output)
            self.assertIn('FAIL', output)
            self.assertIn('ERROR COPY END', output)
            self.assertNotIn('\x1b', output)

    def test_heartbeat_is_not_worker_activity_and_age_does_not_repeat_errors(self):
        result = {'projects': [{'project': 'test', 'path': '/test', 'status': 'RUNNING',
                               'updated_seconds_ago': 120}]}
        first = signature(result)
        result['projects'][0]['updated_seconds_ago'] = 122
        self.assertEqual(first, signature(result))
        self.assertNotEqual(heartbeat(result, 0), heartbeat(result, 1))
        self.assertIn('живость исполнителя не подтверждена', render(result))
        result['projects'][0]['updated_seconds_ago'] = 59
        self.assertNotEqual(first, signature(result))
        result['projects'][0]['status'] = 'BLOCKED'
        self.assertNotEqual(first, signature(result))

    def test_live_clock_ticks_do_not_force_a_full_refresh(self):
        result = {'projects': [{'project': 'test', 'path': '/test', 'status': 'RUNNING',
                                'updated_seconds_ago': 4,
                                'live': {'alive': True, 'activity': 'read files', 'events': 2,
                                         'heartbeat_age': 3, 'event_age': 10}}]}
        first = signature(result)
        result['projects'][0]['live'].update(heartbeat_age=4, event_age=11)
        result['projects'][0]['updated_seconds_ago'] = 5
        self.assertEqual(first, signature(result))
        result['projects'][0]['live']['event_age'] = 61
        self.assertNotEqual(first, signature(result))

    def test_wait_duration_advances_without_rewriting_state(self):
        result = {'projects': [{'project': 'waiter', 'path': '/waiter', 'status': 'WAITING_FOR_SLOT',
                                'updated_seconds_ago': 12,
                                'guard': {'wait_seconds': 7, 'owners': []}}]}
        self.assertIn('Ожидание слота: 0 мин 19 с', render(result))
        first = signature(result)
        result['projects'][0]['updated_seconds_ago'] = 14
        self.assertIn('Ожидание слота: 0 мин 21 с', render(result))
        self.assertEqual(first, signature(result))
