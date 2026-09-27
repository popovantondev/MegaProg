"""JSON-lines bridge from the Node MCP host to the fixed advisory boundary.

This is deliberately not an MCP server.  It owns one ``AdvisoryGateway`` for
the lifetime of the Node process and accepts only the two already-registered
operations over its private stdin/stdout pipe.
"""
import json
import sys
from pathlib import Path

from .mcp_advisory import AdvisoryGateway

MAX_REQUEST_BYTES = 1024 * 1024


def _reply(value):
    sys.stdout.write(json.dumps(value, ensure_ascii=False, separators=(',', ':')) + '\n')
    sys.stdout.flush()


def serve(root, input_stream=None):
    """Serve the private bridge; malformed or unknown requests fail closed."""
    gateway = AdvisoryGateway(root)
    stream = input_stream if input_stream is not None else sys.stdin.buffer
    for raw in stream:
        if len(raw) > MAX_REQUEST_BYTES:
            _reply({"ok": False, "error": "bridge request exceeds byte limit"})
            continue
        try:
            request = json.loads(raw.decode("utf-8"))
            if not isinstance(request, dict) or set(request) != {"tool", "arguments"}:
                raise ValueError("bridge request must contain only tool and arguments")
            if request["tool"] == "get_review_batch":
                result = gateway.get_review_batch(request["arguments"])
            elif request["tool"] == "submit_review_batch":
                result = gateway.submit_review_batch(request["arguments"])
            else:
                raise ValueError("bridge tool is outside the fixed advisory contract")
            _reply({"ok": True, "result": result})
        except (UnicodeDecodeError, ValueError, TypeError) as exc:
            _reply({"ok": False, "error": str(exc)})


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2 or args[0] != "--root":
        raise SystemExit("usage: python -m ai_dev.mcp_bridge --root PROJECT_ROOT")
    root = Path(args[1]).resolve()
    if not root.is_dir():
        raise SystemExit("project root does not exist")
    serve(root)
    return 0


if __name__ == "__main__":
    main()
