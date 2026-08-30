# receipt.ai

A record of how one person actually corrects an AI, and a ruleset built from it where every rule has to show where it came from.

## Where this came from

Between March 2025 and August 2026 — 21 months — I kept correcting the same assistant. Not in a study, just in the normal course of using it for work, music, a job search, a car repair, finding somewhere to live.

To build this I went back through that history: date-sampled sweeps of about 100 chats, plus targeted probes for correction-shaped moments across the full record. Every correction that turned up got a date and the words I actually used.

That produced three things.

**A taxonomy** — six categories the corrections sort into, built bottom-up from the record rather than from a framework I picked first. Each carries a status: resolved, recurring, or installed-but-unproven. Items that were really product limitations rather than behavior are excluded on purpose; they belong in a build backlog, not here.

**A ruleset** — the rules that came out of it, currently v1.1. Every rule carries its receipt inline:

- `[M: date]` — a dated correction from the personal record
- `[E: label]` — external evidence, with its quality labeled
- `[M+E]` — both
- `[★]` — no external twin. Not present anywhere in the published guard collections or the repair literature I surveyed.

There are five `[★]` rules. Those are the part that isn't a restatement of advice already circulating.

**An eval set** — ten real asks with their ground truth written down: what a correct answer had to *contain*, not just how it had to sound. The public copy here is obfuscated. Exact figures are ranges, a specific vehicle is "an older hybrid," particular people are role-generic. Cities, genres and event types stay, because removing them would make the goals meaningless. The full-detail version stays private and is not in this repository.

## What the record says, including the unflattering parts

Some things went well. Corrections became standards that held — the October 2025 "unknown over guess" correction turned into a written research brief that still works. Mode misses stopped after one correction and didn't come back. I invented context-splitting for myself about four months before I wrote it down as a rule.

Some things didn't. Length is the longest-running unfixed problem in the record: four dated "make it shorter" corrections between March 2025 and June 2026, and the same request for inline prose five separate times through August 2026. Fidelity errors still land even with guards installed. One delivery failure — files reported as sent that never arrived — cost enough trust that I now write the guard in capital letters.

And one thing the record honestly can't settle: corrections got denser through 2026, but I was also asking for harder work over the same period. Both moved at once, and there isn't enough evidence before March 2025 to establish a baseline. So I can't tell you whether behavior got worse or the scope got heavier, and I'm not going to pretend otherwise.

## Contents

- [`/taxonomy`](./taxonomy) — the six categories and the dated correction ledger behind them
- [`/ruleset`](./ruleset) — baseline ruleset v1.1, receipts inline
- [`/eval-set`](./eval-set) — ten asks with ground truth, obfuscated

## Status

The taxonomy and ruleset are v1 and v1.1. The eval set is v1. An app that puts the rules, an audit lane, the daily operating system, and notes aggregation behind one door is being built; it is not finished, and nothing here claims it is.
