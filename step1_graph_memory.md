# Step 1: Graph-Based Memory Implementation

## Overview
The foundational upgrade to achieve a fully autonomous, proactive JARVIS is replacing flat, linear memory storage with a Graph-Based Memory architecture. This transforms how the system stores, retrieves, and reasons over long-term information.

## The Problem with Flat Memory
Currently, memory operates as a list of isolated facts (e.g., via `remember`/`recall`). While functional, it lacks relational context. If you mention "Shyam" and later "the email draft," a flat system treats them as separate strings. It cannot natively infer that Shyam is the intended recipient of the draft without explicit prompting every time.

## What is Graph-Based Memory?
Graph memory structures data as **Nodes** (entities, concepts, events) and **Edges** (relationships between them). 
- **Nodes:** Krithik, Shyam, SVCE, JARVIS MARK 3, Spotify Playlist, Morph API Key.
- **Edges:** `is_friend_of`, `studies_at`, `authored_by`, `prefers_listening_to`, `requires_key`.

Instead of saving a sentence, the system extracts the semantic triple: `(Krithik) -[is_friend_of]-> (Shyam)`.

## Why This is the First Step
Autonomy requires context. A proactive assistant must anticipate needs based on interconnected facts rather than waiting for exact keyword matches. Graph memory enables:
1. **Multi-hop Reasoning:** "Who was that friend I wanted to impress with my email project?" -> Traverses `friend` -> `Shyam` -> `impress_goal` -> `Stark Surprise draft`.
2. **Decay & Reinforcement:** Frequently accessed edges strengthen; obsolete ones fade, keeping the knowledge base relevant without manual cleanup.
3. **Proactive Triggering:** When a new node (e.g., an incoming email from Tejas) connects to an existing subgraph (Morph API key), JARVIS can autonomously surface related pending tasks.

## Implementation Plan
1. **Entity Extraction:** Run NLP/LLM parsing on all `remember` calls and conversation logs to identify nodes and edges automatically.
2. **Storage Layer:** Utilize a lightweight local graph database (e.g., Neo4j, SQLite with recursive CTEs, or NetworkX in-memory for smaller graphs) stored within `C:\Users\krith\mark3\`.
3. **Retrieval Augmentation:** Modify the `recall` tool to perform subgraph traversal instead of vector similarity search alone, returning rich contextual clusters.
4. **Visualization (Optional):** Generate local HTML/SVG snapshots of the knowledge graph for debugging and review.

## Expected Outcome
Once implemented, JARVIS will transition from a reactive lookup table to an associative reasoning engine, forming the bedrock for subsequent upgrades like the Autonomous Agent Swarm and Proactive Sentinel Loop.