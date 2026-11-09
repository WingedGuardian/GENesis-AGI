"""Real file loaders and wrappers; synthetic provider/CC/PageIndex boundaries."""

import importlib
from types import SimpleNamespace


async def configure_tools(root, service, record):
    if service == "memory":
        documents = importlib.import_module("genesis.mcp.memory.documents")
        knowledge = importlib.import_module("genesis.mcp.memory.knowledge")
        from genesis.knowledge import tree_index
        from genesis.knowledge.applications import resume_review
        from genesis.runtime._core import GenesisRuntime

        tree_index._INDICES_DIR = root / "ordinary-indices"

        class PageIndex:
            async def upload_document(self, path):
                from pathlib import Path

                record("upload", text=Path(path).read_text())
                return "pi-caller-fixture"

            async def get_tree(self, doc_id):
                return {"result": [{"title": "Synthetic", "page_index": 1}]}

            async def query_document(self, doc_id, question):
                record("query", doc_id=doc_id, question=question)
                return {"choices": [{"message": {"content": "Synthetic answer"}}]}

        class Reviewer:
            def __init__(self, **kwargs):
                record("reviewer")

            async def review(self, text, **kwargs):
                record("resume", text=text)
                return SimpleNamespace(pass1_analysis=text, pass2_augmented="synthetic",
                                       combined_output=text, knowledge_citations=[], error=None)

        documents._get_tree_client = PageIndex
        resume_review.ResumeReviewer = Reviewer
        GenesisRuntime.instance = classmethod(lambda cls: SimpleNamespace(_router=object()))
        modules = {"document_index": documents, "document_query": documents,
                   "resume_review": knowledge}
    else:
        from genesis.eval.skill_replay import persist
        from genesis.eval.skill_replay import runner as replay
        from genesis.experimentation import evo, persistence, runner
        from genesis.learning.skills import skill_gate_config, wiring

        evo_tool = importlib.import_module("genesis.mcp.health.evo_run")
        experiment_tool = importlib.import_module("genesis.mcp.health.experiment_run")
        replay_tool = importlib.import_module("genesis.mcp.health.skill_replay_run")
        task_tool = importlib.import_module("genesis.mcp.health.task_tools")
        health = importlib.import_module("genesis.mcp.health_mcp")
        health._service = SimpleNamespace(_db=object())

        class Generator:
            async def route_call(self, call_site, messages):
                record("generation", text=messages[-1]["content"])
                return SimpleNamespace(success=True, content="Synthetic reflection")

            async def close(self):
                pass

        class Judge:
            async def score_async(self, *args, **kwargs):
                return True, .8, "{}"

        class Invoker:
            async def run(self, invocation):
                record("replay", text=invocation.prompt)
                return SimpleNamespace(is_error=False, text="Synthetic replay", model_used="sonnet",
                                       duration_ms=0, cost_usd=0, input_tokens=0, output_tokens=0)

        async def persist_only(*args, **kwargs):
            record("persistence")  # No experiment/replay write outside the owned DB.

        experiment = runner.run_reflection_experiment

        async def injected_experiment(**kwargs):
            return await experiment(**{**kwargs, "gen_router": Generator(), "judge": Judge()})

        replay_run = replay.run_skill_replay

        async def injected_replay(**kwargs):
            return await replay_run(**kwargs, verify_prod=False, invoker=Invoker(), scorer=Judge(),
                                    run_root=root / "replay-runs")

        runner.run_reflection_experiment = injected_experiment
        replay.run_skill_replay = injected_replay
        # The executor is injected above: no native Claude process authenticates.
        # Keep task loading, invocation construction and paired runner intact.
        def bare_configuration(run_dir):
            directory = run_dir / "synthetic-cc-config"
            directory.mkdir()
            return directory

        replay.prepare_bare_config_dir = bare_configuration
        evo_tool._build_routers = lambda *args: (Generator(), Generator(), Judge())
        persistence.persist_experiment = persist_only
        evo.persist_evo_summary = persist_only
        persist.persist_skill_replay_summary = persist.log_replay_observation = persist_only
        skill_gate_config.skill_replay_mode = lambda: "shadow"
        wiring.get_skill_path = lambda name: root / "fixture-skill.md"
        modules = {"evo_run": evo_tool, "experiment_run": experiment_tool,
                   "skill_replay_run": replay_tool, "task_submit": task_tool}
    tools = {}
    for name, module in modules.items():
        tool = await module.mcp.get_tool(name)
        assert tool.fn.__module__ == module.__name__, (name, tool.fn.__module__)
        tools[name] = tool
    return tools
