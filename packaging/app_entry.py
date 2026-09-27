"""PyInstaller entry point: show the approved-plan GUI or dispatch its CLI."""
import sys

if len(sys.argv) > 1 and sys.argv[1] == "--cli":
    from ai_dev.cli import main
    raise SystemExit(main(sys.argv[2:]))

from ai_dev.plan_gui import main
main()
