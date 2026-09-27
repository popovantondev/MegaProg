import sys

if len(sys.argv) > 1 and sys.argv[1] == '--cli':
    from .cli import main
    raise SystemExit(main(sys.argv[2:]))

from .gui import main
main()
