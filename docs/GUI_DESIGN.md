# MegaProg desktop redesign

Approved direction: a five-step Qt wizard (Project → Connection → Plan → Run → Result),
with a centred four-node/check icon, restrained depth, system/light/dark themes and
complete RU/DE/EN interface text. Keep approved-plan schema v1 and its existing
execution, budget, verification and continuation rules.

Acceptance: reading a plan executes nothing; approval binds to the reviewed bytes
and project; no automatic restart after failure; completion needs backend proof.
Show current task, completed task count, actual turn budget, live elapsed time,
and last observed activity. Missing usage is unknown, never zero or invented savings.
Select saved plans explicitly. Stop shows stopping until the process actually exits.
Export the existing portable handoff for manual attachment in ChatGPT.

Visual rules: 1120×780 initial, 860×620 minimum, one main action per page, 8px
spacing rhythm, 16px cards, 10px controls, system font, readable 14–15pt body.
Test RU/DE/EN × light/dark × normal/minimum, plus large type, keyboard focus,
long names and all stop/failure states. Use synthetic fixtures and offline workers.

Implementation uses PySide6 6.10.2 Widgets and the existing CLI in a child process.
No embedded web engine, new planner, paid API, model calls for previews, or state migration.
The previous candidate and installed app remain available for rollback. Publication
requires final review of the new exact candidate.

User refinement for 4.5.1: pair the two opposite mint nodes at a smaller size and the two opposite turquoise nodes at a larger size. Keep ring, check, tile and overall centering. Folder validation explains missing Git history, nested folders, unavailable Git and permissions separately, preserving the selected path on failure.
