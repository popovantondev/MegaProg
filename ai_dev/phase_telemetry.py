"""Best-effort monotonic wall-clock measurements for local task phases."""

import time
from datetime import datetime, timezone


def _utc_now():
    return datetime.now(timezone.utc).isoformat()


def timestamp():
    return _utc_now()


class Phase:
    def __init__(self, state, name, category='local_tool', overlap_group=None):
        self.state = state
        self.name = name
        self.category = category
        self.overlap_group = overlap_group
        self.started_perf = None
        self.row = None

    def __enter__(self):
        self.started_perf = time.perf_counter()
        self.row = {'phase': self.name, 'category': self.category,
                    'started_at': _utc_now(), 'finished_at': None,
                    'duration_ms': None, 'status': 'RUNNING'}
        if self.overlap_group:
            self.row['overlap_group'] = self.overlap_group
        self.state.setdefault('phase_telemetry', []).append(self.row)
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.row['duration_ms'] = max(0, round((time.perf_counter() - self.started_perf) * 1000))
        self.row['finished_at'] = _utc_now()
        self.row['status'] = 'FAILED' if exc_type else 'COMPLETED'
        refresh_summary(self.state)
        return False


def measure(state, name, category='local_tool', overlap_group=None):
    return Phase(state, name, category, overlap_group)


def record(state, name, started_perf, started_at, category='local_tool', status='COMPLETED'):
    row = {'phase': name, 'category': category,
           'started_at': started_at, 'finished_at': _utc_now(),
           'duration_ms': max(0, round((time.perf_counter() - started_perf) * 1000)),
           'status': status}
    state.setdefault('phase_telemetry', []).append(row)
    refresh_summary(state)
    return row


def refresh_summary(state):
    rows = [item for item in state.get('phase_telemetry', [])
            if isinstance(item, dict) and type(item.get('duration_ms')) is int]
    totals = {key: sum(item['duration_ms'] for item in rows if item.get('category') == category)
              for key, category in (('model_wall_time_ms', 'model'),
                                    ('local_tool_wall_time_ms', 'local_tool'),
                                    ('test_wall_time_ms', 'test'),
                                    ('slot_wait_wall_time_ms', 'wait'))}
    state['wall_time'] = dict(totals, total_wall_time_ms=state.get('total_wall_time_ms'),
        category_totals_overlap_possible=True,
        note='Phase/category durations are measurements, not additive components of total wall time; nested phases may overlap.')
