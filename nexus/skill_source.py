from __future__ import annotations

from ironman.skills.source import SkillMeta, SkillSource
from nexus.skills import SkillRegistry


class NexusSkillSource:
    def __init__(self, registry: SkillRegistry, source: str = "nexus") -> None:
        self._registry = registry
        self._source = source

    @property
    def registry(self) -> SkillRegistry:
        return self._registry

    def list_skills(self) -> list[SkillMeta]:
        metas: list[SkillMeta] = []
        for view in self._registry.list_meta():
            metas.append(
                SkillMeta(
                    name=view.name,
                    description=view.description,
                    source=view.source or self._source,
                    metadata={
                        "dir_name": view.dir_name,
                        "compatibility": view.compatibility,
                        "license": view.license,
                        "spec_findings": list(view.spec_findings),
                    },
                )
            )
        return metas

    def load(self, name: str) -> str | None:
        return self._registry.get_body(name)

    def suggest(self, query: str, top_k: int = 5) -> list[SkillMeta]:
        return [
            SkillMeta(
                name=view.name,
                description=view.description,
                source=view.source or self._source,
                metadata={"score": score},
            )
            for view, score in self._registry.suggest(query, top_k=top_k)
        ]

    def read_resource(self, name: str, rel_path: str) -> str:
        return self._registry.read_resource(name, rel_path)


def build_nexus_skill_source(registry: SkillRegistry, source: str = "nexus") -> SkillSource:
    return NexusSkillSource(registry, source=source)
