# Step 1: Graph-Based Memory Module

## Objective
Transition JARVIS from linear, flat-text memory retrieval to a semantic graph structure. This allows the system to understand relationships between entities (files, people, tasks, concepts) rather than just matching keywords.

## Why This Matters
- **Contextual Awareness:** Instead of just knowing "Shyam is a friend," the graph links Shyam -> Email -> Drafts -> Projects.
- **Proactive Suggestions:** If you open a Python file, the graph can suggest related test scripts or documentation you viewed last week.
- **Reduced Hallucinations:** Facts are stored as nodes with verified edges, reducing the chance of mixing up unrelated details.

## Technical Architecture
1. **Node Types:** 
   - `Person` (e.g., Shyam, Tejas)
   - `File` (e.g., changes.txt, tes2342.py)
   - `Task` (e.g., Daily Briefing, mail-watch)
   - `Concept` (e.g., Graph Memory, JEV AI)
2. **Edge Types:** 
   - `RELATED_TO`, `OWNED_BY`, `MODIFIED_ON`, `DISCUSSED_IN`
3. **Storage:** Local SQLite database with vector embeddings for semantic search on node descriptions.
4. **Integration:** Hook into the `remember` and `recall` tools to automatically update the graph when new facts are stored.

## Implementation Plan
1. **Schema Design:** Define the SQL schema for nodes and edges.
2. **Embedding Service:** Integrate a lightweight local embedding model (e.g., all-MiniLM-L6-v2) to generate vector representations for node content.
3. **Write Hook:** Modify the `remember` tool to parse text for entities and create/update nodes in the graph.
4. **Read Hook:** Modify the `recall` tool to perform a hybrid search: vector similarity + graph traversal (e.g., "Find files related to Shyam").

## Expected Outcome
A persistent, queryable knowledge graph that grows smarter with every interaction, enabling true proactive assistance.