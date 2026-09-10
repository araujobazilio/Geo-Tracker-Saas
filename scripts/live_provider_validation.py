"""Plan or execute one explicitly authorized live provider validation.

The default is PLAN ONLY.  A live call requires both ``--execute-live`` and
the exact acknowledgement string defined by the operator service.  This
script has no retry, rerun, worker, Celery, or provider-specific bypass flag.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

from app.config import get_settings
from app.core.enums import LLMProvider
from app.core.exceptions import AppError
from app.db.session import get_session_factory
from app.services.live_accounting_validation_operator import (
    LIVE_PROVIDER_CALL_ACK,
    LiveAccountingValidationOperator,
    LiveValidationRequest,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Plan or execute one isolated OpenAI live accounting validation."
    )
    parser.add_argument("--workspace-id", required=True, type=uuid.UUID)
    parser.add_argument("--validation-id", required=True)
    parser.add_argument("--prompt-file", required=True, type=Path)
    parser.add_argument("--evidence-dir", required=True, type=Path)
    parser.add_argument("--provider", required=True, choices=[LLMProvider.OPENAI.value])
    parser.add_argument(
        "--execute-live",
        action="store_true",
        help="Enable the single live provider call; otherwise the command is PLAN ONLY.",
    )
    parser.add_argument(
        "--acknowledge-paid-provider-call",
        default="",
        help="Required exact acknowledgement when --execute-live is supplied.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.execute_live and args.acknowledge_paid_provider_call != LIVE_PROVIDER_CALL_ACK:
        print(
            "Blocked: --execute-live requires the exact paid-call acknowledgement.",
            file=sys.stderr,
        )
        return 2
    if not args.execute_live and args.acknowledge_paid_provider_call:
        print(
            "Blocked: paid-call acknowledgement is only valid with --execute-live.",
            file=sys.stderr,
        )
        return 2

    try:
        operator = LiveAccountingValidationOperator(
            get_session_factory(),
            settings=get_settings(),
        )
        prompt = operator.load_prompt_file(args.prompt_file)
        request = LiveValidationRequest(
            workspace_id=args.workspace_id,
            validation_id=args.validation_id,
            prompt_file=args.prompt_file,
            evidence_dir=args.evidence_dir,
            provider=LLMProvider(args.provider),
        )
        if args.execute_live:
            report = operator.execute(
                request,
                prompt,
                acknowledgement=args.acknowledge_paid_provider_call,
            )
            payload = report.as_dict()
            exit_code = 0
        else:
            plan = operator.plan(request, prompt)
            payload = plan.as_dict()
            exit_code = 0
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return exit_code
    except (AppError, OSError, ValueError) as exc:
        message = getattr(exc, "message", str(exc))
        print(f"Blocked: {message}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
