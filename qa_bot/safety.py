"""Production guard, checked before every ``exec``.

It runs every time because the human (or an earlier command) can switch the kube
context or database env mid-session. It looks at:

- the command text itself (``--context prod-…``, ``-h db.prod…``, prod URLs),
- the pane shell's env as published by the hooks at the last prompt
  (``KUBECONFIG``, ``PGHOST``, ``PGDATABASE``, ``PGSERVICE``, ``DATABASE_URL``),
- the active kube context, cluster and server for the pane's ``KUBECONFIG``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

DEFAULT_PROD_PATTERN = r"(?<![a-z0-9])(prod|production|prd)(?![a-z])"
DB_ENV_KEYS = ("PGHOST", "PGDATABASE", "PGSERVICE", "DATABASE_URL")


class ProductionRefused(RuntimeError):
    pass


def prod_pattern() -> re.Pattern[str]:
    return re.compile(os.environ.get("QA_BOT_PROD_PATTERN", DEFAULT_PROD_PATTERN), re.I)


def read_pane_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    try:
        text = path.read_text()
    except OSError:
        return env
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep and value:
            env[key] = value
    return env


def kube_context(pane_env: dict[str, str]) -> dict[str, str]:
    """Active context/cluster/server/namespace for the pane's kubeconfig ({} if none)."""
    kubectl = shutil.which("kubectl")
    if not kubectl:
        return {}
    env = dict(os.environ)
    if pane_env.get("KUBECONFIG"):
        env["KUBECONFIG"] = pane_env["KUBECONFIG"]
    else:
        env.pop("KUBECONFIG", None)
    try:
        result = subprocess.run(
            [kubectl, "config", "view", "--minify", "-o", "json"],
            capture_output=True,
            text=True,
            timeout=10,
            env=env,
            cwd=pane_env.get("PWD") or None,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}
    if result.returncode != 0:  # e.g. no current context set
        return {}
    try:
        cfg = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {}
    info = {"context": cfg.get("current-context", "")}
    for ctx in cfg.get("contexts") or []:
        info["cluster"] = (ctx.get("context") or {}).get("cluster", "")
        info["namespace"] = (ctx.get("context") or {}).get("namespace", "")
    for cluster in cfg.get("clusters") or []:
        info["server"] = (cluster.get("cluster") or {}).get("server", "")
    return {k: v for k, v in info.items() if v}


def check_not_production(command: str, pane_env: dict[str, str], kube: dict[str, str]) -> None:
    """Raise ProductionRefused if the command or the active context looks like production."""
    pattern = prod_pattern()
    if m := pattern.search(command):
        raise ProductionRefused(
            f"Refused: the command mentions '{m.group(0)}', which looks like production."
        )
    for key, value in kube.items():
        if pattern.search(value):
            raise ProductionRefused(
                f"Refused: active kube {key} '{value}' looks like production. "
                "Switch context in the pane first."
            )
    for key in DB_ENV_KEYS:
        value = pane_env.get(key, "")
        if value and (m := pattern.search(value)):
            # Don't echo the value: DATABASE_URL may contain a password.
            raise ProductionRefused(
                f"Refused: {key} in the pane contains '{m.group(0)}', which looks like production."
            )
