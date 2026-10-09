"""CLI boundary; import Planner directly from a future web UI."""
import argparse
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

from agent.planner import Planner


EXAMPLE = {"city": "Bangalore", "budget": 2000, "available_time": "4 hours",
           "mood": "tired but wants to do something fun", "interests": ["food", "music", "walks"],
           "constraints": ["vegetarian", "avoid crowded places"]}


def main():
    load_dotenv()
    parser = argparse.ArgumentParser(description="Perfect Saturday Planner backend (INR)")
    parser.add_argument("--input", type=Path, help="JSON input file; defaults to the assignment example")
    parser.add_argument("--json", dest="raw_json", help="JSON input string, or '-' to read stdin")
    parser.add_argument("--mock", action="store_true", help="Explicit offline demo: no LLM, no network or API keys")
    parser.add_argument("--mock-places", action="store_true", help="Real LLM with mock venue and route providers")
    args = parser.parse_args()
    try:
        if args.input and args.raw_json:
            raise ValueError("Choose --input or --json")
        if args.input:
            data = json.loads(args.input.read_text(encoding="utf-8"))
        elif args.raw_json:
            data = json.loads(sys.stdin.read() if args.raw_json == "-" else args.raw_json)
        else:
            data = EXAMPLE
        result = Planner(places_mode="mock" if args.mock_places else "auto").plan(data, offline=args.mock)
        print(result.model_dump_json(indent=2))
        return 0 if result.status in {"success", "conditional"} else 1
    except (OSError, ValueError):
        print(json.dumps({"status": "failure", "message": "Could not read input JSON. Check path, encoding and JSON syntax.", "trace": []}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
