"""Numeric-precedence config layering with per-key origin tracking (Codex pattern).

Codex composes its effective configuration from several named sources — system
defaults, enterprise policy, the signed-in user, the active profile, the project
repo, and the live session — each with a fixed numeric precedence. Higher
precedence wins per leaf key, and every value in the final merge remembers which
layer contributed it so ``/debug-config`` can explain *why* a setting is what it
is.

This module is the pure layering primitive: no I/O, no schema validation, no
effort resolution (that lives elsewhere). Stdlib only.
"""
from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import ClassVar


@dataclass
class Layer:
    """One config layer.

    ``precedence`` is a small integer; higher beats lower. The built-in ranks
    follow Codex's ordering and are exposed as class constants so callers write
    ``Layer.PROJECT`` rather than a magic number.
    """

    # Built-in precedence ranks (ClassVar -> not dataclass fields).
    SYSTEM: ClassVar[int] = 10
    ENTERPRISE: ClassVar[int] = 15
    USER: ClassVar[int] = 20
    PROFILE: ClassVar[int] = 21
    PROJECT: ClassVar[int] = 25
    SESSION: ClassVar[int] = 30

    name: str
    source: str
    precedence: int
    data: dict


@dataclass
class ConfigStack:
    """An ordered set of :class:`Layer` instances merged by precedence.

    Layers are merged lowest→highest precedence; nested dicts merge recursively,
    lists and scalars are replaced (not concatenated). Every leaf key tracks the
    name of the layer that won it, for explainability.

    Tie-break: when two layers share a precedence, the one added later wins
    (Python's sort is stable, so insertion order survives the precedence sort).
    """

    # Identity of the auto-created session-override layer.
    _OVERRIDE_NAME: ClassVar[str] = "session"
    _OVERRIDE_SOURCE: ClassVar[str] = "override"

    layers: list[Layer] = field(default_factory=list)

    def add(self, layer: Layer) -> None:
        """Register a layer. Later ``add`` calls break precedence ties."""
        self.layers.append(layer)

    def merge(self) -> dict:
        """Deep-merge all layers into one dict (higher precedence wins)."""
        merged, _ = self._merge_and_origins()
        return merged

    def origins(self) -> dict[str, str]:
        """Map each leaf dotted-path to the winning layer's ``name``.

        Only leaves of the *final* merged structure appear — if a higher layer
        replaces a whole dict with a scalar, the lower layer's sub-paths under
        it are dropped (they no longer exist in the effective config).
        """
        _, origins = self._merge_and_origins()
        return origins

    def fingerprint(self, layer_name: str) -> str:
        """sha256 of the named layer's canonical (sorted-keys) JSON.

        Covers the whole layer (name, source, precedence, data), so it is stable
        across runs for identical content and differs on any change — used to
        detect config drift between sessions.
        """
        layer = self._find(layer_name)
        canonical = json.dumps(
            asdict(layer), sort_keys=True, ensure_ascii=False, separators=(",", ":")
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def set_override(self, dotted_path: str, value: object) -> None:
        """Write ``value`` at ``dotted_path`` into a SESSION layer.

        A SESSION-precedence layer (named "session") is created if none exists,
        so the override always wins over lower-ranked layers.
        """
        layer = self._find_by_precedence(Layer.SESSION)
        if layer is None:
            layer = Layer(
                name=self._OVERRIDE_NAME,
                source=self._OVERRIDE_SOURCE,
                precedence=Layer.SESSION,
                data={},
            )
            self.layers.append(layer)
        _write_dotted(layer.data, dotted_path, value)

    # -- internals --
    def _find(self, name: str) -> Layer:
        for layer in self.layers:
            if layer.name == name:
                return layer
        raise KeyError(f"no layer named {name!r}")

    def _find_by_precedence(self, precedence: int) -> Layer | None:
        for layer in self.layers:
            if layer.precedence == precedence:
                return layer
        return None

    def _merge_and_origins(self) -> tuple[dict, dict[str, str]]:
        merged: dict = {}
        origins: dict[str, str] = {}
        for layer in sorted(self.layers, key=lambda l: l.precedence):
            _merge_into(merged, layer.data, "", layer.name, origins)
        return merged, origins


def _merge_into(
    dst: dict, src: dict, prefix: str, origin: str, origins: dict[str, str]
) -> None:
    """Deep-merge ``src`` into ``dst`` in place; track leaf origins.

    - nested dicts merge recursively (lower-precedence leaves that survive stay);
    - lists and scalars replace, clearing any stale sub-leaves under that path;
    - ``origin`` is recorded for every leaf written.
    """
    for key, src_val in src.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(src_val, dict):
            if key in dst and isinstance(dst[key], dict):
                # both dicts -> descend, preserving surviving lower-precedence leaves
                _merge_into(dst[key], src_val, path, origin, origins)
            else:
                # src replaces (or is new): clear stale leaves, reset container, recurse
                _clear_prefix(origins, path)
                dst[key] = {}
                _merge_into(dst[key], src_val, path, origin, origins)
        else:
            # scalar/list leaf replaces whatever was at `path`
            _clear_prefix(origins, path)
            dst[key] = copy.deepcopy(src_val)
            origins[path] = origin


def _clear_prefix(origins: dict[str, str], path: str) -> None:
    """Drop ``path`` itself and any ``path.*`` entries from ``origins``."""
    prefix_dot = path + "."
    for key in list(origins.keys()):
        if key == path or key.startswith(prefix_dot):
            del origins[key]


def _write_dotted(data: dict, dotted_path: str, value: object) -> None:
    """Write ``value`` into ``data`` at ``dotted_path``, creating intermediate dicts."""
    parts = dotted_path.split(".")
    target = data
    for part in parts[:-1]:
        if not isinstance(target.get(part), dict):
            target[part] = {}
        target = target[part]
    target[parts[-1]] = copy.deepcopy(value)
