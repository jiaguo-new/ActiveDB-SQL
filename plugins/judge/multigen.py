"""MultiGen: Route A tournament on a second candidate pool (triple_merged).
Runs after the first Route A judge to try recovering more questions."""
from __future__ import annotations
import sys
from pathlib import Path

# This plugin reuses the same judge logic but with a different pool
def create_plugin(config: dict, ctx) -> callable:
    root = ctx.get("root", Path("."))
    sys.path.insert(0, str(root))

    # Import the route_a plugin factory and create an instance with a different pool
    from plugins.judge.route_a import create_plugin as create_judge
    inner = create_judge(config, ctx)

    return inner
