"""조각 임베딩 (0009 실험). chunk.embed_text → 벡터 → chunk_embedding.

모델은 설정 이름(kure-v3 / kure-v1 / bge-m3)으로 고르고, 실제 HF 모델 id와 차원은 MODELS에 있다. 세 후보는 모두 로컬 모델이다
(사용자 결정 2026-11-24: 1차 비교에서 OpenAI 임베딩 API는 제외 — 유료, 공시 본문을 외부로 보냄, GPU가 있어 필요 없음).

sentence-transformers·torch는 노트북에만 설치한다(requirements-ml.txt). 이 모듈은 그 패키지를 **함수 안에서** import하므로
CI와 웹 환경에서도 import할 수 있고, 테스트는 FakeEmbedder 같은 가짜 임베더를 넣어 돈다.

멱등성: embed_chunks는 (chunk_id, model_name)이 없거나 text_hash가 바뀐 조각만 계산한다. 다시 돌려도 이미 있는 것은 건너뛴다.

CLI (노트북, API 호출 없음, GPU 사용):
    python -m src.rag.embedding build --model kure-v3 --strategy section_path          # 그 전략의 조각 전부 임베딩
    python -m src.rag.embedding build --model kure-v1 --strategy section_path --limit 2000   # 시간 재기용
    python -m src.rag.embedding stats                                                # 모델·전략별 임베딩 수
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass
from typing import Protocol

import duckdb

from src.loaders.duckdb_store import connect


@dataclass(frozen=True)
class ModelSpec:
    hf_id: str
    dim: int                     # 저장하는 차원 (truncate_dim). 모델 원래 차원이 더 크면 앞에서 자른다 (Matryoshka)
    max_seq_length: int          # 조각은 1,000자(≈600토큰)라 이보다 길게 둘 이유가 없다. 길이를 줄이면 속도가 빨라진다
    note: str


MODELS: dict[str, ModelSpec] = {
    "kure-v3": ModelSpec("nlpai-lab/KURE-v3", 1024, 1024, "단일 벡터 1.1B, 원래 2048차원을 1024로 잘라 씀 (계획: 성능 99% 유지). 모델 카드의 query prompt 확인 필요"),
    "kure-v1": ModelSpec("nlpai-lab/KURE-v1", 1024, 1024, "bge-m3를 한국어로 미세조정, 568M"),
    "bge-m3": ModelSpec("BAAI/bge-m3", 1024, 1024, "다국어 기준선, 568M"),
}


class Embedder(Protocol):
    name: str
    dim: int

    def encode(self, texts: list[str], batch_size: int = 32, is_query: bool = False) -> list[list[float]]: ...


class LocalEmbedder:
    """sentence-transformers 로컬 모델. GPU가 있으면 자동으로 쓴다. 정규화된 벡터를 돌려주므로 코사인 = 내적."""

    def __init__(self, name: str, device: str | None = None):
        if name not in MODELS:
            raise ValueError(f"모르는 임베딩 모델: {name} (가능: {', '.join(MODELS)})")
        self.name = name
        self.spec = MODELS[name]
        self.dim = self.spec.dim
        from sentence_transformers import SentenceTransformer  # 노트북 전용 패키지 — 함수 안 import

        self.model = SentenceTransformer(self.spec.hf_id, device=device, truncate_dim=self.spec.dim)
        self.model.max_seq_length = min(self.model.max_seq_length or self.spec.max_seq_length, self.spec.max_seq_length)
        # 모델이 질문용 프롬프트를 정해 두었으면(config의 prompts.query) 질문에만 붙인다. 없으면 그대로
        prompts = getattr(self.model, "prompts", None) or {}
        self.query_prompt_name = "query" if "query" in prompts else None

    def encode(self, texts: list[str], batch_size: int = 32, is_query: bool = False) -> list[list[float]]:
        kwargs = {"batch_size": batch_size, "normalize_embeddings": True, "convert_to_numpy": True, "show_progress_bar": False}
        if is_query and self.query_prompt_name:
            kwargs["prompt_name"] = self.query_prompt_name
        vectors = self.model.encode(texts, **kwargs)
        return [[float(x) for x in v] for v in vectors]


def pending_chunks(con: duckdb.DuckDBPyConnection, model_name: str, strategy: str, limit: int | None = None) -> list[tuple[str, str, str]]:
    """아직 임베딩이 없거나 해시가 바뀐 조각: [(chunk_id, text_hash, embed_text)]."""
    sql = """
        SELECT c.chunk_id, c.text_hash, c.embed_text
        FROM chunk c LEFT JOIN chunk_embedding e ON e.chunk_id = c.chunk_id AND e.model_name = ?
        WHERE c.strategy = ? AND (e.chunk_id IS NULL OR e.text_hash <> c.text_hash)
        ORDER BY c.rcept_no, c.file_name, c.seq
    """
    params: list = [model_name, strategy]
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    return con.execute(sql, params).fetchall()


def embed_chunks(con: duckdb.DuckDBPyConnection, embedder: Embedder, strategy: str, batch_size: int = 32,
                 limit: int | None = None, log=print) -> dict:
    """전략의 조각을 임베딩해 chunk_embedding에 넣는다. 돌려주는 값: {"pending", "embedded", "seconds", "per_1000"}."""
    todo = pending_chunks(con, embedder.name, strategy, limit)
    stats = {"model": embedder.name, "strategy": strategy, "pending": len(todo), "embedded": 0, "seconds": 0.0, "per_1000": None}
    if not todo:
        return stats
    t0 = time.perf_counter()
    for start in range(0, len(todo), batch_size * 8):  # 배치 몇 개마다 한 번씩 DB에 쓴다 (중간에 끊겨도 그만큼은 남는다)
        part = todo[start: start + batch_size * 8]
        vectors = embedder.encode([t[2] for t in part], batch_size=batch_size)
        ids = [t[0] for t in part]
        con.begin()
        try:
            con.execute("DELETE FROM chunk_embedding WHERE model_name = ? AND chunk_id IN (SELECT unnest(?::VARCHAR[]))", [embedder.name, ids])
            con.executemany("INSERT INTO chunk_embedding VALUES (?, ?, ?, ?, ?, now())",
                            [[cid, embedder.name, h, len(v), v] for (cid, h, _), v in zip(part, vectors)])
            con.commit()
        except Exception:
            con.rollback()
            raise
        stats["embedded"] += len(part)
        elapsed = time.perf_counter() - t0
        log(f"  {stats['embedded']:,}/{len(todo):,}  {elapsed:,.0f}초  (1,000개당 {elapsed / stats['embedded'] * 1000:,.1f}초)")
    stats["seconds"] = round(time.perf_counter() - t0, 1)
    stats["per_1000"] = round(stats["seconds"] / stats["embedded"] * 1000, 1)
    return stats


def embedding_stats(con: duckdb.DuckDBPyConnection) -> list[dict]:
    rows = con.execute(
        """SELECT e.model_name, c.strategy, count(*), any_value(e.dim), count(DISTINCT c.rcept_no),
                  sum(CASE WHEN e.text_hash <> c.text_hash THEN 1 ELSE 0 END)
           FROM chunk_embedding e JOIN chunk c USING (chunk_id) GROUP BY 1, 2 ORDER BY 1, 2"""
    ).fetchall()
    return [{"model": r[0], "strategy": r[1], "embedded": r[2], "dim": r[3], "documents": r[4], "stale": r[5]} for r in rows]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="chunk → chunk_embedding")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--model", required=True, help=" / ".join(MODELS))
    b.add_argument("--strategy", default="section_path")
    b.add_argument("--batch", type=int, default=32)
    b.add_argument("--limit", type=int, help="앞에서 n개만 (시간 재기)")
    b.add_argument("--device", help="cuda / cpu (기본 자동)")
    sub.add_parser("stats")
    args = ap.parse_args(argv)

    if args.cmd == "build":
        embedder = LocalEmbedder(args.model, args.device)
        with connect() as con:
            print(f"모델 {args.model} ({embedder.spec.hf_id}, {embedder.dim}차원), 전략 {args.strategy}, 배치 {args.batch}")
            stats = embed_chunks(con, embedder, args.strategy, args.batch, args.limit)
        print(stats)
        return 0
    with connect(read_only=True) as con:
        rows = embedding_stats(con)
    if not rows:
        print("임베딩이 없습니다. 먼저 build를 실행하세요.")
    for r in rows:
        print(f"{r['model']:<10}{r['strategy']:<14}{r['embedded']:>8,}개  {r['dim']}차원  보고서 {r['documents']}  해시 바뀐 것 {r['stale']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
