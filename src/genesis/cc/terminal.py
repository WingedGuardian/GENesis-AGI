"""Terminal entry point for Genesis conversation.

Bootstraps the full GenesisRuntime so all subsystems are active.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from genesis.cc.conversation import ConversationLoop
from genesis.cc.system_prompt import SystemPromptAssembler
from genesis.cc.types import ChannelType, StreamEvent
from genesis.runtime import GenesisRuntime

_BANNER = """\
Genesis Terminal — type your message, press Enter.
Commands: /model <sonnet|opus|haiku>, /effort <low|medium|high>
Type 'exit' or 'quit' to leave. Ctrl+D or Ctrl+C also works.
"""


async def run_terminal(
    *,
    user_id: str = "local",
    day_boundary_hour: int = 0,
    verbose: bool = False,
) -> None:
    runtime = GenesisRuntime.instance()
    await runtime.bootstrap()

    if not runtime.is_bootstrapped or runtime.cc_invoker is None:
        print("GenesisRuntime bootstrap failed — check logs")
        return

    try:
        assembler = SystemPromptAssembler()

        # Inline failure detector
        failure_detector = None
        try:
            from genesis.learning.failure_detector import FailureDetector
            failure_detector = FailureDetector()
        except Exception:
            pass

        loop = ConversationLoop(
            db=runtime.db,
            invoker=runtime.cc_invoker,
            assembler=assembler,
            day_boundary_hour=day_boundary_hour,
            triage_pipeline=runtime.triage_pipeline,
            context_injector=runtime.context_injector,
            contingency=runtime.contingency_dispatcher,
            failure_detector=failure_detector,
        )

        print(_BANNER)

        aloop = asyncio.get_event_loop()
        while True:
            try:
                text = (await aloop.run_in_executor(None, input, "You: ")).strip()
            except EOFError:
                print()
                break

            if not text:
                continue
            if text.lower() in ("exit", "quit"):
                break

            if verbose:
                print("[sending to CC...]")

            async def _on_event(event: StreamEvent) -> None:
                if event.event_type == "tool_use" and event.tool_name:
                    print(f"\r  [{event.tool_name}...]", end="", flush=True)
                elif event.event_type == "text" and event.text:
                    sys.stdout.write(event.text)
                    sys.stdout.flush()

            response = await loop.handle_message_streaming(
                text,
                user_id=user_id,
                channel=ChannelType.TERMINAL,
                on_event=_on_event,
            )
            # Newline after streaming output, then formatted final
            print(f"\nGenesis: {response}\n")
    finally:
        await runtime.shutdown()


def main() -> None:
    parser = argparse.ArgumentParser(description="Genesis terminal conversation")
    parser.add_argument("--user", default="local", help="User ID (default: local)")
    parser.add_argument(
        "--boundary-hour", type=int, default=0,
        help="UTC hour for morning reset (default: 0)",
    )
    parser.add_argument("--verbose", action="store_true", help="Show debug info")
    args = parser.parse_args()

    from genesis.util.process_lock import ProcessLock

    try:
        with ProcessLock("terminal"):
            asyncio.run(
                run_terminal(
                    user_id=args.user,
                    day_boundary_hour=args.boundary_hour,
                    verbose=args.verbose,
                )
            )
    except KeyboardInterrupt:
        print("\nBye.")


if __name__ == "__main__":
    main()
