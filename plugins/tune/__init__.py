#!/usr/bin/env python3
"""Tune-stage plugin base.

Tune plugins run OFFLINE (before the inference pipeline). They produce
artifacts — evolved prompts, retrained weights, new candidate pools — that
downstream inference plugins consume via ctx.

Contract:
    create_tuner(config, ctx) -> tune(train_iter) -> artifacts dict

Compliance (enforced by the runner):
    - The engine only passes TRAIN data iterators to tune plugins.
    - A tune plugin whose config references dev/test data is rejected
      by the source guard before loading.
"""
from __future__ import annotations
from typing import Callable, Iterable

TuneFn = Callable[[Iterable[dict]], dict]


def create_tuner(config: dict, ctx) -> TuneFn:
    """Override in each tune plugin. Returns a function train_iter -> artifacts."""
    raise NotImplementedError
