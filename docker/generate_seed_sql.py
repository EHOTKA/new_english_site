#!/usr/bin/env python3
"""
Generate seed_global_lemmas.sql for the global_lemmas table.

Extracts the top 10,000 English lemmas by subtitle frequency (OpenSubtitles 50k)
enriched with parts of speech (POS) and CEFR levels (A1-C2) from the curated
CEFR dataset (word_cefr_minified.db).

Usage:
    python3 generate_seed_sql.py [--limit 10000] [--output seed_global_lemmas.sql]
"""

import os
import re
import sqlite3
import argparse
import collections
from pathlib import Path

# Paths to datasets
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "word_cefr_minified.db"
SUB_PATH = DATA_DIR / "en_50k.txt"

# Penn Treebank tag -> simplified POS
PENN_TO_POS = {
    'NN': 'noun', 'NNS': 'noun',
    'VB': 'verb', 'VBD': 'verb', 'VBG': 'verb', 'VBN': 'verb', 'VBP': 'verb', 'VBZ': 'verb', 'MD': 'verb',
    'JJ': 'adjective', 'JJR': 'adjective', 'JJS': 'adjective',
    'RB': 'adverb', 'RBR': 'adverb', 'RBS': 'adverb', 'WRB': 'adverb',
    'PRP': 'pronoun', 'PRP$': 'pronoun', 'WP': 'pronoun', 'WP$': 'pronoun',
    'IN': 'preposition', 'CC': 'conjunction',
    'DT': 'determiner', 'WDT': 'determiner', 'PDT': 'determiner',
    'CD': 'numeral', 'UH': 'interjection',
    'RP': 'particle', 'TO': 'preposition',
}

# Subtitle / stemming artifacts to exclude
BLACKLIST = {
    'um', 'uh', 'tion', 'sci', 'ing', 'est', 'ly', 'al', 'th', 'cit', 'et', 'ond', 'thi', 'oo'
}


def float_to_cefr(lvl: float | None) -> str:
    """Map continuous numeric CEFR level to standard CEFR level enum."""
    if lvl is None or lvl <= 0:
        return 'unknown'
    if lvl < 1.5:
        return 'A1'
    if lvl < 2.5:
        return 'A2'
    if lvl < 3.5:
        return 'B1'
    if lvl < 4.5:
        return 'B2'
    if lvl < 5.5:
        return 'C1'
    return 'C2'


def load_dataset(limit: int = 10_000) -> list[dict]:
    """Extract and rank the top N clean lemmas with POS and CEFR levels."""
    if not DB_PATH.exists():
        raise FileNotFoundError(f"Database file not found: {DB_PATH}")
    if not SUB_PATH.exists():
        raise FileNotFoundError(f"Subtitles frequency file not found: {SUB_PATH}")

    print(f"Loading SQLite CEFR dataset from {DB_PATH.name}...")
    conn = sqlite3.connect(DB_PATH)

    words = dict(conn.execute("SELECT word_id, word FROM words"))
    tag_names = dict(conn.execute("SELECT tag_id, tag FROM pos_tags"))
    rows = conn.execute("SELECT word_id, pos_tag_id, lemma_word_id, frequency_count, level FROM word_pos").fetchall()

    # Identify common words (exclude words dominated by proper nouns NNP/NNPS)
    word_nnp_freq = collections.defaultdict(int)
    word_non_nnp_freq = collections.defaultdict(int)
    for wid, tag_id, lemma_id, freq, level in rows:
        tag = tag_names.get(tag_id, '')
        if tag in ('NNP', 'NNPS'):
            word_nnp_freq[wid] += freq
        else:
            word_non_nnp_freq[wid] += freq

    common_words = set()
    for wid, non_nnp in word_non_nnp_freq.items():
        nnp = word_nnp_freq[wid]
        if non_nnp > 0 and (non_nnp / (nnp + non_nnp)) >= 0.5:
            common_words.add(wid)

    # Resolve form -> lemma mapping
    lemma_pos_freq = collections.defaultdict(collections.Counter)
    lemma_cefr_scores = collections.defaultdict(list)
    form_lemma_votes = collections.defaultdict(collections.Counter)

    for wid, tag_id, lemma_id, freq, level in rows:
        target_id = lemma_id if (lemma_id and lemma_id in common_words) else wid
        form_lemma_votes[wid][target_id] += freq

    form_to_lemma = {wid: max(votes, key=votes.get) for wid, votes in form_lemma_votes.items()}

    # Collect POS distributions and CEFR ratings for each canonical lemma
    for wid, tag_id, lemma_id, freq, level in rows:
        resolved_id = form_to_lemma[wid]
        tag = tag_names.get(tag_id, '')
        if tag not in ('NNP', 'NNPS'):
            pos = PENN_TO_POS.get(tag)
            if pos:
                lemma_pos_freq[resolved_id][pos] += freq
        if level and level > 0:
            lemma_cefr_scores[resolved_id].append((level, freq))

    word_to_id = {w: wid for wid, w in words.items()}

    # Aggregate subtitle frequencies from OpenSubtitles 50k
    print(f"Reading subtitle frequencies from {SUB_PATH.name}...")
    sub_lemma_freq = collections.Counter()
    with open(SUB_PATH, encoding='utf-8') as f:
        for line in f:
            parts = line.strip().split()
            if len(parts) != 2:
                continue
            form = parts[0].lower()
            if not re.fullmatch(r"[a-z]+", form):
                continue
            freq = int(parts[1])
            wid = word_to_id.get(form)
            if wid:
                resolved_id = form_to_lemma.get(wid, wid)
                sub_lemma_freq[resolved_id] += freq

    # Build and filter candidate lemmas
    candidates = []
    for lid, total_sub_freq in sub_lemma_freq.items():
        if lid not in common_words:
            continue
        lemma = words.get(lid, '').lower()
        if not lemma or lemma in BLACKLIST:
            continue
        if len(lemma) == 1 and lemma not in ('a', 'i'):
            continue
        if not re.fullmatch(r"[a-z]+", lemma):
            continue

        pos_counts = lemma_pos_freq[lid]
        if not pos_counts:
            continue
        dominant_pos = max(pos_counts, key=pos_counts.get)

        scores = lemma_cefr_scores[lid]
        if scores:
            tot_w = sum(w for _, w in scores)
            avg_lvl = sum(lvl * w for lvl, w in scores) / tot_w if tot_w > 0 else scores[0][0]
        else:
            avg_lvl = None

        cefr = float_to_cefr(avg_lvl)
        candidates.append({
            "sub_freq": total_sub_freq,
            "lemma": lemma,
            "part_of_speech": dominant_pos,
            "cefr_level": cefr,
        })

    # Sort descending by frequency in subtitles
    candidates.sort(key=lambda x: x["sub_freq"], reverse=True)
    top_lemmas = candidates[:limit]

    # Assign sequential 1-based frequency ranks
    for rank, item in enumerate(top_lemmas, start=1):
        item["frequency_rank"] = rank

    # Print summary statistics
    print(f"\nExtracted {len(top_lemmas)} lemmas (target: {limit})")
    pos_counts = collections.Counter(x["part_of_speech"] for x in top_lemmas)
    print("POS Distribution:")
    for pos, count in pos_counts.most_common():
        print(f"  {pos:<15}: {count}")

    cefr_counts = collections.Counter(x["cefr_level"] for x in top_lemmas)
    print("CEFR Distribution:")
    for cefr, count in cefr_counts.most_common():
        print(f"  {cefr:<15}: {count}")

    return top_lemmas


def escape_sql_string(s: str) -> str:
    """Escape single quotes for SQL literals."""
    return s.replace("'", "''")


def generate_sql(lemmas: list[dict], output_file: Path, batch_size: int = 500):
    """Write SQL statements to an output file."""
    lines = [
        "-- Auto-generated seed for global_lemmas table",
        f"-- Total lemmas: {len(lemmas)}",
        "",
        "BEGIN;",
        "",
        "-- Clear existing data",
        "DELETE FROM global_lemmas;",
        "",
        "-- Bulk insert",
    ]

    for i in range(0, len(lemmas), batch_size):
        batch = lemmas[i:i + batch_size]
        values = []
        for e in batch:
            lemma = escape_sql_string(e["lemma"])
            pos = escape_sql_string(e["part_of_speech"])
            cefr = e["cefr_level"]
            rank = e["frequency_rank"]
            values.append(f"('{lemma}', '{pos}', '{cefr}'::cefr_level_enum, {rank})")
        values_str = ",\n".join(values)
        lines.append(
            f"INSERT INTO global_lemmas (lemma, part_of_speech, cefr_level, frequency_rank)\nVALUES\n{values_str};"
        )
        lines.append("")

    lines.append("COMMIT;")
    lines.append("")
    lines.append("-- Verification queries")
    lines.append("SELECT COUNT(*) AS total_lemmas FROM global_lemmas;")
    lines.append("SELECT lemma, part_of_speech, cefr_level, frequency_rank FROM global_lemmas ORDER BY frequency_rank LIMIT 10;")
    lines.append("SELECT part_of_speech, COUNT(*) FROM global_lemmas GROUP BY part_of_speech ORDER BY COUNT(*) DESC;")
    lines.append("SELECT cefr_level, COUNT(*) FROM global_lemmas GROUP BY cefr_level ORDER BY cefr_level;")

    with open(output_file, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"\nSuccessfully wrote {output_file} ({len(lemmas)} lemmas, batches of {batch_size}).")


def main():
    parser = argparse.ArgumentParser(description="Generate seed_global_lemmas.sql")
    parser.add_argument("--limit", type=int, default=10_000, help="Number of lemmas to seed")
    parser.add_argument("--output", default="seed_global_lemmas.sql", help="Output SQL filename")
    parser.add_argument("--batch-size", type=int, default=500, help="Rows per INSERT statement")
    args = parser.parse_args()

    out_path = BASE_DIR / args.output
    lemmas = load_dataset(limit=args.limit)
    generate_sql(lemmas, output_file=out_path, batch_size=args.batch_size)


if __name__ == "__main__":
    main()
