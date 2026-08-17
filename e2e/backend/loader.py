"""Discover test definitions: YAML files and Python escape-hatch modules."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import yaml

from . import primitives
from .model import VERBS, Step, Test


def _load_yaml_tests(bioflow_dir: Path) -> list[Test]:
    out: list[Test] = []
    for path in sorted(bioflow_dir.glob("*.yaml")) + sorted(bioflow_dir.glob("*.yml")):
        data = yaml.safe_load(path.read_text()) or {}
        name = data.get("name")
        if not name:
            raise ValueError(f"{path}: missing 'name'")
        steps: list[Step] = []
        for item in data.get("steps", []):
            if not isinstance(item, dict) or len(item) != 1:
                raise ValueError(f"{path}: each step must be a single-key mapping")
            verb, args = next(iter(item.items()))
            if verb not in VERBS:
                raise ValueError(f"{path}: unknown verb {verb!r} (allowed: {sorted(VERBS)})")
            steps.append(Step(verb=verb, args=args or {}))
        out.append(Test(
            name=str(name), kind="yaml",
            description=str(data.get("description", "")), steps=steps,
        ))
    return out


def _load_python_tests(bioflow_dir: Path) -> list[Test]:
    out: list[Test] = []
    for path in sorted(bioflow_dir.glob("*.py")):
        mod_name = f"bioflow_e2e_{path.stem}"
        before = set(primitives._REGISTRY)
        spec = importlib.util.spec_from_file_location(mod_name, path)
        if spec is None or spec.loader is None:
            raise ImportError(f"could not load {path}")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = mod
        spec.loader.exec_module(mod)
        for name in set(primitives._REGISTRY) - before:
            reg = primitives._REGISTRY[name]
            out.append(Test(name=name, kind="python", description=reg.description, callable=reg.fn))
    return out


def discover_tests(tests_dir: str) -> list[Test]:
    bioflow_dir = Path(tests_dir) / "bioflow"
    return _load_yaml_tests(bioflow_dir) + _load_python_tests(bioflow_dir)
