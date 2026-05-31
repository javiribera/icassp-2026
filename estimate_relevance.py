#!/usr/bin/env python3
"""
Scores the relevance of each paper in papers.json using the Anthropic API.

Reads PROMPT_FOR_RELEVANCE.txt (edit it to describe your research interests),
then asks Claude to score each paper from 0 to 100.

Produces papers_with_relevance.json and papers_with_relevance.csv.

Works with papers.json from any conference (ICASSP, CVPR, ICCV, WACV).

Requires the ANTHROPIC_API_KEY environment variable.

Usage:
  python estimate_relevance.py                              # Batches API (default)
  python estimate_relevance.py --no-batch --limit 5        # real-time, 5 papers
  python estimate_relevance.py --input data/papers.json    # custom input
"""

import argparse
import concurrent.futures
import csv
import json
import logging
import re
import time
from pathlib import Path

import anthropic
from tqdm import tqdm

DEFAULT_MODEL = "claude-haiku-4-5"
DEFAULT_WORKERS = 8
CHECKPOINT_FILE = "relevance_checkpoint.json"
CHECKPOINT_INTERVAL = 200
PROMPT_FILE = "PROMPT_FOR_RELEVANCE.txt"
POLL_INTERVAL = 60  # seconds between batch status polls

# (model-id-prefix, (input $/1M tokens, output $/1M tokens))
MODEL_PRICING: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-opus-4-6": (5.00, 25.00),
    "claude-opus-4-7": (5.00, 25.00),
}

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)


class UsageSummary:
    def __init__(self) -> None:
        self.input_tokens = 0
        self.output_tokens = 0

    def add(self, input_tokens: int, output_tokens: int) -> None:
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens

    def report(self, model: str, batch: bool = False) -> None:
        if not self.input_tokens and not self.output_tokens:
            return
        total = self.input_tokens + self.output_tokens
        log.info(
            f"Tokens used: {self.input_tokens:,} input + {self.output_tokens:,} output"
            f" = {total:,} total"
        )
        pricing = next(
            (v for k, v in MODEL_PRICING.items() if model.startswith(k)), None
        )
        if pricing:
            in_rate, out_rate = pricing
            if batch:
                # Anthropic Batches API is billed at 50% of standard token rates.
                in_rate, out_rate = in_rate * 0.5, out_rate * 0.5
            cost = (
                self.input_tokens * in_rate + self.output_tokens * out_rate
            ) / 1_000_000
            suffix = f"{model}, Batches API −50%" if batch else model
            log.info(f"Estimated cost: ${cost:.4f} ({suffix})")
        else:
            log.info(f"Cost estimate unavailable for model {model!r} (not in pricing table)")


# ---------------------------------------------------------------------------
# Checkpoint
# ---------------------------------------------------------------------------

def load_checkpoint(
    output_dir: Path,
) -> tuple[dict[str, int], str | None, list[str]]:
    """Returns (scored_map, batch_id_or_None, batch_titles)."""
    cp_path = output_dir / CHECKPOINT_FILE
    if not cp_path.exists():
        return {}, None, []
    try:
        data = json.loads(cp_path.read_text())
        return (
            data.get("scored", {}),
            data.get("batch_id"),
            data.get("batch_titles", []),
        )
    except Exception as e:
        log.warning(f"Could not read checkpoint ({e}); starting fresh.")
        return {}, None, []


def save_checkpoint(
    output_dir: Path,
    scored: dict[str, int],
    batch_id: str | None = None,
    batch_titles: list[str] | None = None,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    cp_path = output_dir / CHECKPOINT_FILE
    try:
        cp_path.write_text(
            json.dumps(
                {
                    "scored": scored,
                    "batch_id": batch_id,
                    "batch_titles": batch_titles or [],
                },
                ensure_ascii=False,
            )
        )
    except OSError as e:
        log.warning(f"Could not save checkpoint: {e}")


# ---------------------------------------------------------------------------
# Prompt / scoring helpers
# ---------------------------------------------------------------------------

def build_prompt(user_prompt: str, paper: dict) -> str:
    title = paper.get("title", "")
    abstract = paper.get("abstract", "")
    return (
        f"{user_prompt}\n\n"
        f"---\n"
        f"Title: {title}\n"
        f"Abstract: {abstract}\n\n"
        f"Respond with a single integer from 0 to 100 representing your relevance score."
    )


def parse_score(text: str, label: str = "") -> int:
    m = re.search(r"\b(\d{1,3})\b", text)
    if m:
        return max(0, min(100, int(m.group(1))))
    log.warning(
        f"No integer in model response{f' for {label!r}' if label else ''}; "
        f"defaulting to 0. Got: {text!r}"
    )
    return 0


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def write_output(papers: list[dict], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "papers_with_relevance.json"
    json_path.write_text(json.dumps(papers, indent=2, ensure_ascii=False))
    csv_path = output_dir / "papers_with_relevance.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f, fieldnames=["title", "abstract", "url", "relevance"]
        )
        writer.writeheader()
        writer.writerows(
            {
                "title": p.get("title", ""),
                "abstract": p.get("abstract", ""),
                "url": p.get("url", ""),
                "relevance": p.get("relevance", 0),
            }
            for p in papers
        )
    log.info(f"JSON → {json_path}")
    log.info(f"CSV  → {csv_path}")


# ---------------------------------------------------------------------------
# Batches API mode
# ---------------------------------------------------------------------------

def score_batch(
    client: anthropic.Anthropic,
    papers: list[dict],
    scored: dict[str, int],
    user_prompt: str,
    model: str,
    output_dir: Path,
    use_checkpoint: bool,
    cp_batch_id: str | None,
    cp_batch_titles: list[str],
    usage: UsageSummary,
) -> dict[str, int]:
    to_score = [p for p in papers if p.get("title", "") not in scored]
    if not to_score:
        log.info("All papers already scored.")
        return scored

    # Resume an existing batch if we have its ID from a previous run
    if cp_batch_id and cp_batch_titles:
        log.info(f"Resuming batch {cp_batch_id} ...")
        batch = client.messages.batches.retrieve(cp_batch_id)
        to_score_titles = cp_batch_titles
    else:
        to_score_titles = [p.get("title", "") for p in to_score]
        log.info(f"Submitting batch of {len(to_score)} papers...")
        batch = client.messages.batches.create(
            requests=[
                {
                    "custom_id": str(i),
                    "params": {
                        "model": model,
                        "max_tokens": 16,
                        "messages": [
                            {
                                "role": "user",
                                "content": build_prompt(user_prompt, p),
                            }
                        ],
                    },
                }
                for i, p in enumerate(to_score)
            ]
        )
        log.info(f"Batch submitted: {batch.id}")
        if use_checkpoint:
            save_checkpoint(output_dir, scored, batch.id, to_score_titles)

    # Poll until done
    while batch.processing_status != "ended":
        counts = batch.request_counts
        log.info(
            f"Batch {batch.id}: {batch.processing_status} "
            f"(succeeded={counts.succeeded}, errored={counts.errored}, "
            f"processing={counts.processing})"
        )
        time.sleep(POLL_INTERVAL)
        batch = client.messages.batches.retrieve(batch.id)

    log.info(f"Batch {batch.id} complete. Processing results...")
    for result in client.messages.batches.results(batch.id):
        idx = int(result.custom_id)
        title = to_score_titles[idx] if idx < len(to_score_titles) else ""
        if result.result.type == "succeeded":
            msg = result.result.message  # type: ignore[union-attr]
            usage.add(msg.usage.input_tokens, msg.usage.output_tokens)
            scored[title] = parse_score(msg.content[0].text, label=title)  # type: ignore[union-attr]
        else:
            log.warning(
                f"Batch result for paper {idx} ({result.result.type}); defaulting to 0."
            )
            scored[title] = 0

    return scored


# ---------------------------------------------------------------------------
# Real-time concurrent mode
# ---------------------------------------------------------------------------

def score_realtime(
    client: anthropic.Anthropic,
    papers: list[dict],
    scored: dict[str, int],
    user_prompt: str,
    model: str,
    workers: int,
    output_dir: Path,
    use_checkpoint: bool,
    usage: UsageSummary,
) -> dict[str, int]:
    to_score = [p for p in papers if p.get("title", "") not in scored]
    if not to_score:
        log.info("All papers already scored.")
        return scored

    log.info(f"Scoring {len(to_score)} papers with {workers} workers (real-time)...")
    completed = 0

    def _task(paper: dict) -> tuple[str, int, int, int]:
        prompt = build_prompt(user_prompt, paper)
        title = paper.get("title", "")
        try:
            resp = client.messages.create(
                model=model,
                max_tokens=16,
                messages=[{"role": "user", "content": prompt}],
            )
            text = resp.content[0].text  # type: ignore[union-attr]
            return title, parse_score(text, label=title), resp.usage.input_tokens, resp.usage.output_tokens
        except Exception as e:
            log.warning(f"Error scoring '{title}': {e}")
            return title, 0, 0, 0

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futs = [pool.submit(_task, p) for p in to_score]
        for fut in tqdm(
            concurrent.futures.as_completed(futs), total=len(to_score), desc="Scoring"
        ):
            title, score, inp, out = fut.result()
            scored[title] = score
            usage.add(inp, out)
            completed += 1
            if use_checkpoint and completed % CHECKPOINT_INTERVAL == 0:
                save_checkpoint(output_dir, scored)

    return scored


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Score relevance of papers in papers.json using the Anthropic API.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=Path("output/papers.json"),
        metavar="PATH",
        help="Input papers.json (default: output/papers.json)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        metavar="DIR",
        help="Output directory (default: same directory as input)",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        metavar="ID",
        help=f"Claude model to use (default: {DEFAULT_MODEL})",
    )
    parser.add_argument(
        "--no-batch",
        action="store_true",
        help="Use real-time concurrent API calls instead of the Batches API",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        metavar="N",
        help=f"Concurrent workers (only with --no-batch; default: {DEFAULT_WORKERS})",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="Score only the first N papers (for testing; disables checkpointing)",
    )
    args = parser.parse_args()

    output_dir = args.output if args.output is not None else args.input.parent
    use_checkpoint = args.limit is None

    if not args.input.exists():
        log.error(f"{args.input} not found.")
        raise SystemExit(1)
    papers: list[dict] = json.loads(args.input.read_text())
    log.info(f"Loaded {len(papers)} papers from {args.input}")

    if args.limit is not None:
        papers = papers[: args.limit]

    prompt_path = Path(PROMPT_FILE)
    if not prompt_path.exists():
        log.error(
            f"{PROMPT_FILE} not found.\n"
            "  Create this file and describe your research interests.\n"
            "  Example: 'I am a researcher in speech synthesis and neural TTS.'"
        )
        raise SystemExit(1)
    user_prompt = prompt_path.read_text().strip()

    scored: dict[str, int] = {}
    cp_batch_id: str | None = None
    cp_batch_titles: list[str] = []
    if use_checkpoint:
        scored, cp_batch_id, cp_batch_titles = load_checkpoint(output_dir)
        if scored:
            log.info(f"Resuming: {len(scored)} papers already scored.")

    usage = UsageSummary()
    client = anthropic.Anthropic()
    try:
        if args.no_batch:
            scored = score_realtime(
                client,
                papers,
                scored,
                user_prompt,
                args.model,
                workers=args.workers,
                output_dir=output_dir,
                use_checkpoint=use_checkpoint,
                usage=usage,
            )
        else:
            scored = score_batch(
                client,
                papers,
                scored,
                user_prompt,
                args.model,
                output_dir=output_dir,
                use_checkpoint=use_checkpoint,
                cp_batch_id=cp_batch_id,
                cp_batch_titles=cp_batch_titles,
                usage=usage,
            )

        for paper in papers:
            title = paper.get("title", "")
            paper["relevance"] = scored.get(title, 0)

        write_output(papers, output_dir)

        if use_checkpoint:
            cp_path = output_dir / CHECKPOINT_FILE
            if cp_path.exists():
                cp_path.unlink()
                log.info("Checkpoint removed.")

        log.info(f"Done. {len(papers)} papers scored.")
    finally:
        usage.report(args.model, batch=not args.no_batch)


if __name__ == "__main__":
    main()
