"""approach_18 — Intelligent inference: clustering, view forecasting,
best posting time, and recommendations. Stdlib only.

No sklearn / Prophet dependency: TF-IDF + K-Means, linear-trend
forecasting with residual-based confidence intervals, and hour/day
engagement maximisation are implemented in pure Python so the module
runs in CI. Every output is a suggestion with a confidence / interval;
nothing here ever posts, likes, or otherwise acts on an account —
auto-posting is explicitly out of scope.

Fix (fix_import_dual_contracts_residual): exactly ONE import style
for the contract — ``from src.contracts import ...`` run from the
repo root. The old ``try/except ImportError`` fallback (``from
contracts import``) is removed: a fallback can load the same file as
a second, distinct module object (``contracts`` vs ``src.contracts``),
so Enum/dataclass identity checks (``is``, ``isinstance``) silently
break across modules.
"""
from __future__ import annotations

import math
import random
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Optional

# DUY NHAT mot cach import contract: tu repo root. Khong fallback.
from src.contracts import (
    Platform, Recommendation, UnifiedVideoMetric, Video, ViewForecast,
    compute_engagement_rate, utcnow,
)

__all__ = [
    "Cluster", "tokenize", "tfidf_vectors", "kmeans", "cluster_videos",
    "forecast_views", "best_posting_hours", "recommend",
]

_STOPWORDS = {"va", "la", "cua", "cho", "voi", "the", "a", "an", "and", "or", "to", "of", "in", "on", "is", "are"}
_TOKEN_RE = re.compile(r"[a-z0-9_]+", re.I)


def tokenize(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN_RE.findall(text or "") if t.lower() not in _STOPWORDS and len(t) > 1]


def tfidf_vectors(texts: list[str]) -> tuple[list[dict[str, float]], list[str]]:
    docs = [tokenize(t) for t in texts]
    df = Counter()
    for d in docs:
        for tok in set(d):
            df[tok] += 1
    n = max(1, len(docs))
    vocab = sorted(df)
    idf = {t: math.log((n + 1) / (df[t] + 1)) + 1.0 for t in vocab}
    vectors: list[dict[str, float]] = []
    for d in docs:
        counts = Counter(d)
        total = max(1, len(d))
        vec = {t: (counts[t] / total) * idf[t] for t in counts}
        norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
        vectors.append({t: v / norm for t, v in vec.items()})
    return vectors, vocab


def _cosine(a: dict[str, float], b: dict[str, float]) -> float:
    if len(a) > len(b):
        a, b = b, a
    return sum(v * b.get(k, 0.0) for k, v in a.items())


def _dense(vec: dict[str, float], vocab: list[str]) -> list[float]:
    return [vec.get(t, 0.0) for t in vocab]


def kmeans(vectors: list[list[float]], k: int, *, seed: int = 42,
           max_iter: int = 100) -> tuple[list[int], list[list[float]]]:
    """Deterministic K-Means (first-k distinct points as seeds)."""
    if not vectors or k <= 0:
        raise ValueError("no vectors / k <= 0")
    k = min(k, len(vectors))
    rng = random.Random(seed)
    centroids = [list(vectors[i]) for i in range(k)]
    labels = [0] * len(vectors)
    for _ in range(max_iter):
        new_labels = []
        for v in vectors:
            dists = [sum((a - b) ** 2 for a, b in zip(v, c)) for c in centroids]
            new_labels.append(min(range(k), key=lambda i: (dists[i], i)))
        if new_labels == labels:
            break
        labels = new_labels
        for j in range(k):
            members = [vectors[i] for i, lab in enumerate(labels) if lab == j]
            if members:
                centroids[j] = [sum(col) / len(members) for col in zip(*members)]
    return labels, centroids


@dataclass
class Cluster:
    id: int
    video_ids: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    avg_engagement: float = 0.0
    avg_views: float = 0.0


def cluster_videos(videos: list[Video], metrics: dict[str, UnifiedVideoMetric],
                   *, k: int = 3) -> list[Cluster]:
    """Cluster by title+description TF-IDF. Returns clusters ranked by avg engagement."""
    if not videos:
        return []
    texts = [f"{v.title} {v.description}" for v in videos]
    sparse, vocab = tfidf_vectors(texts)
    dense = [_dense(v, vocab) for v in sparse]
    labels, _ = kmeans(dense, k)
    groups: dict[int, list[int]] = defaultdict(list)
    for i, lab in enumerate(labels):
        groups[lab].append(i)
    out: list[Cluster] = []
    for cid, idxs in groups.items():
        kw_counts = Counter()
        for i in idxs:
            for tok, w in sparse[i].items():
                kw_counts[tok] += w
        engs = [compute_engagement_rate(metrics[videos[i].id].views, metrics[videos[i].id].likes,
                                        metrics[videos[i].id].comments, metrics[videos[i].id].shares)
                for i in idxs if videos[i].id in metrics]
        views = [metrics[videos[i].id].views for i in idxs if videos[i].id in metrics]
        out.append(Cluster(id=cid, video_ids=[videos[i].id for i in idxs],
                           keywords=[t for t, _ in kw_counts.most_common(5)],
                           avg_engagement=sum(engs) / len(engs) if engs else 0.0,
                           avg_views=sum(views) / len(views) if views else 0.0))
    return sorted(out, key=lambda c: c.avg_engagement, reverse=True)


def _linear_fit(xs: list[float], ys: list[float]) -> tuple[float, float]:
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    denom = sum((x - mx) ** 2 for x in xs) or 1.0
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / denom
    return slope, my - slope * mx


def forecast_views(history: list[UnifiedVideoMetric], *, horizon: str = "24h",
                   video_id: str = "") -> ViewForecast:
    """Forecast cumulative views: linear trend on log(views) vs hours,
    with a residual-based interval. Confidence falls with horizon/spread."""
    hist = sorted(history, key=lambda m: m.collected_at)
    vid = video_id or (hist[0].video_id if hist else "")
    model = "linear_trend_log_views"
    if len(hist) < 3:
        last = hist[-1].views if hist else 0
        return ViewForecast(video_id=vid, horizon=horizon, predicted_views=float(last),
                            lower_bound=float(last), upper_bound=float(last),
                            confidence=0.2, model=model)
    t0 = hist[0].collected_at
    xs = [(m.collected_at - t0).total_seconds() / 3600.0 for m in hist]
    ys = [math.log(max(1, m.views)) for m in hist]
    slope, intercept = _linear_fit(xs, ys)
    resid = [y - (slope * x + intercept) for x, y in zip(xs, ys)]
    sd = math.sqrt(sum(r * r for r in resid) / len(resid)) if resid else 0.0
    hours = 24.0 if horizon == "24h" else 168.0 if horizon == "7d" else 24.0
    x_future = xs[-1] + hours
    log_pred = slope * x_future + intercept
    pred = max(float(hist[-1].views), math.exp(log_pred))
    width = math.exp(sd * 1.96) if sd else 1.05
    lower = max(float(hist[-1].views), pred / width)
    upper = pred * width
    confidence = max(0.1, min(0.9, 0.85 - sd * 0.5 - (0.15 if horizon == "7d" else 0.0)))
    return ViewForecast(video_id=vid, horizon=horizon, predicted_views=pred,
                        lower_bound=lower, upper_bound=upper,
                        confidence=confidence, model=model)


def best_posting_hours(videos: list[Video], metrics: dict[str, UnifiedVideoMetric],
                       *, top_n: int = 3) -> list[int]:
    """Argmax of smoothed engagement by hour-of-day (pooled across days)."""
    by_hour: dict[int, list[float]] = defaultdict(list)
    all_eng: list[float] = []
    for v in videos:
        m = metrics.get(v.id)
        if not m or not v.published_at:
            continue
        eng = compute_engagement_rate(m.views, m.likes, m.comments, m.shares)
        by_hour[v.published_at.hour].append(eng)
        all_eng.append(eng)
    if not all_eng:
        return []
    global_mean = sum(all_eng) / len(all_eng)
    scored: list[tuple[float, int]] = []
    for h in range(24):
        vals = by_hour.get(h, [])
        smoothed = (sum(vals) + 2 * global_mean) / (len(vals) + 2) if vals else global_mean
        scored.append((smoothed, h))
    scored.sort(reverse=True)
    return [h for _, h in scored[:top_n]]


def recommend(*, account_id: str, videos: list[Video],
              metrics: dict[str, UnifiedVideoMetric],
              peer_videos: Optional[list[Video]] = None,
              peer_metrics: Optional[dict[str, UnifiedVideoMetric]] = None,
              top_n: int = 5) -> list[Recommendation]:
    """Content-based + collaborative recommendations. Returns Recommendation
    objects only — never posts anything."""
    if not videos:
        return []
    ranked = sorted(videos, key=lambda v: compute_engagement_rate(
        metrics[v.id].views, metrics[v.id].likes, metrics[v.id].comments, metrics[v.id].shares)
        if v.id in metrics else 0.0, reverse=True)
    recs: list[Recommendation] = []
    hours = best_posting_hours(videos, metrics)
    if ranked:
        top = ranked[:3]
        clusters = cluster_videos(videos, metrics, k=min(3, len(videos)))
        best_cluster = clusters[0] if clusters else None
        recs.append(Recommendation(
            account_id=account_id,
            title="Double down on your best-performing topic",
            reason=f"Top videos share keywords {best_cluster.keywords if best_cluster else []}; "
                   f"their avg engagement is {(best_cluster.avg_engagement if best_cluster else 0):.3f}. "
                   f"Best examples: {[v.title[:40] for v in top]}",
            confidence=0.7 if best_cluster else 0.4,
            video_ids=[v.id for v in top], best_posting_hours=hours))
    if peer_videos and peer_metrics:
        own_titles = {v.title.lower() for v in videos}
        peers = [v for v in peer_videos if v.title.lower() not in own_titles]
        peers.sort(key=lambda v: compute_engagement_rate(
            peer_metrics[v.id].views, peer_metrics[v.id].likes,
            peer_metrics[v.id].comments, peer_metrics[v.id].shares)
            if v.id in peer_metrics else 0.0, reverse=True)
        if peers:
            recs.append(Recommendation(
                account_id=account_id,
                title="Topics working for peers in the same niche",
                reason="Peer videos with the highest engagement that you have not covered yet.",
                confidence=0.55, video_ids=[v.id for v in peers[:top_n]],
                best_posting_hours=hours))
    growing: list[tuple[float, str]] = []
    by_video: dict[str, list[UnifiedVideoMetric]] = defaultdict(list)
    for m in metrics.values():
        by_video[m.video_id].append(m)
    for vid, hist in by_video.items():
        if len(hist) >= 3:
            fc = forecast_views(hist, horizon="24h", video_id=vid)
            if fc.predicted_views > hist[-1].views:
                growing.append((fc.confidence, vid))
    if growing:
        growing.sort(reverse=True)
        recs.append(Recommendation(
            account_id=account_id,
            title="Analyse your fastest-growing videos first",
            reason="These videos are forecast to gain the most views in the next 24h; study their hook and topic.",
            confidence=growing[0][0], video_ids=[vid for _, vid in growing[:top_n]],
            best_posting_hours=hours))
    return recs[:top_n]
