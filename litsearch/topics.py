"""Versioned research protocols: vocabulary is data, never a global mutable switch."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class Topic:
    id: str
    name: str
    question: str
    products: tuple[str, ...]
    pathway_terms: tuple[str, ...]
    hosts: tuple[str, ...]
    interventions: tuple[str, ...]
    exclusions: tuple[str, ...]
    schema_version: int = 1

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(json.dumps(self.to_dict(), sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def query(self, source: str) -> str:
        # Retrieve broadly; title/abstract screening comes later. Do not AND hosts:
        # doing so silently loses papers whose organism is only in the full text.
        terms = tuple(dict.fromkeys((*self.products, *self.pathway_terms)))
        if source == "pubmed":
            return "(" + " OR ".join(f'"{term}"[Title/Abstract]' for term in terms) + ")"
        if source == "europepmc":
            return "(" + " OR ".join(f'TITLE_ABS:"{term}"' for term in terms) + ")"
        return " OR ".join(f'"{term}"' for term in self.products)


def load_topic(path_or_name: str = "pantothenate") -> Topic:
    path = Path(path_or_name)
    if not path.is_file():
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", path_or_name):
            raise ValueError("课题配置文件不存在")
        path = Path(__file__).with_name("profiles") / f"{path_or_name}.json"
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    allowed = set(Topic.__dataclass_fields__)
    if not isinstance(data, dict) or set(data) != allowed:
        raise ValueError(f"课题配置必须且只能包含：{', '.join(sorted(allowed))}")
    if type(data["schema_version"]) is not int or data["schema_version"] != 1:
        raise ValueError("不支持的课题 schema_version")
    for key in ("id", "name", "question"):
        if not isinstance(data[key], str) or not data[key].strip():
            raise ValueError(f"topic.{key} 必须是非空文本")
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", data["id"]):
        raise ValueError("topic.id 只能使用小写字母、数字、下划线或连字符")
    for key in ("products", "pathway_terms", "hosts", "interventions", "exclusions"):
        values = data[key]
        if not isinstance(values, list) or len(values) > 100:
            raise ValueError(f"topic.{key} 必须是最多100项的文本数组")
        if key in {"products", "hosts", "interventions"} and not values:
            raise ValueError(f"topic.{key} 不能为空")
        for term in values:
            if not isinstance(term, str) or not term.strip() or len(term) > 120:
                raise ValueError(f"topic.{key} 包含空值或无效术语")
            if re.search(r'["\\\r\n\[\]{}:]', term):
                raise ValueError("术语不能包含检索语法；每项仅填写一个词或短语")
        data[key] = tuple(dict.fromkeys(term.strip().casefold() for term in values))
    return Topic(**data)
