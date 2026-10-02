"""Generate the retrieval benchmark corpus and queries.

A small hand-written corpus (40 short technical docs across varied topics) and
30 queries with relevance labels. Labels are graded: 2 = the doc the query is
really about, 1 = a clearly related doc. Keep queries lexically *distinct*
from their target docs where possible (paraphrase, synonyms) so the comparison
between lexical and hashing-dense retrieval is not trivially won by word
overlap. Run ``python benchmarks/datasets/_build_retrieval.py`` to rewrite.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from cairn.eval.dataset import write_jsonl  # noqa: E402

# (id, title, text). Topics: databases, networking, crypto, ML, OS, languages, web.
CORPUS: list[tuple[str, str, str]] = [
    ("d01", "B-tree indexes", "A B-tree keeps keys sorted so a database can find a row with a "
     "logarithmic number of disk reads instead of scanning every page."),
    ("d02", "Write-ahead logging", "Durability comes from recording each change in a log before "
     "the data pages are updated, so a crash can be recovered by replaying the log."),
    ("d03", "MVCC", "Multiversion concurrency control lets readers see a consistent snapshot "
     "while writers create new row versions, avoiding read-write locks."),
    ("d04", "Query planners", "A cost-based optimizer estimates how many rows each operator emits "
     "and picks the join order with the cheapest predicted total cost."),
    ("d05", "Sharding", "Splitting rows across many machines by a partition key spreads load, at "
     "the cost of expensive queries that must touch several partitions."),
    ("d06", "TCP congestion control", "By slowly increasing the sending window and backing off "
     "when packets are lost, endpoints share a link without collapsing it."),
    ("d07", "DNS resolution", "Turning a hostname into an address walks a hierarchy of name "
     "servers, caching answers along the way to cut latency."),
    ("d08", "TLS handshake", "Before encrypting traffic the two sides agree on a cipher and "
     "exchange keys, authenticating the server with a certificate."),
    ("d09", "HTTP caching", "Response headers tell intermediaries how long a document may be "
     "reused, so repeat requests can be answered without contacting the origin."),
    ("d10", "Load balancing", "Spreading incoming connections across a pool of servers keeps any "
     "single machine from being overwhelmed and allows rolling restarts."),
    ("d11", "Public key cryptography", "A keypair lets anyone encrypt with the public half while "
     "only the holder of the private half can decrypt, removing shared secrets."),
    ("d12", "Hash functions", "A good digest maps any input to a fixed-size value such that "
     "finding two inputs with the same output is computationally infeasible."),
    ("d13", "Digital signatures", "Signing a message with a private key lets others verify, with "
     "the matching public key, that it was not altered and who produced it."),
    ("d14", "Symmetric ciphers", "Block ciphers transform fixed-size chunks under a shared key; "
     "a mode of operation chains blocks so identical plaintext differs."),
    ("d15", "Key exchange", "Two parties can derive a shared secret over a public channel so an "
     "eavesdropper who sees every message still cannot compute the key."),
    ("d16", "Gradient descent", "Training nudges parameters in the direction that most reduces "
     "the loss, taking steps proportional to a learning rate."),
    ("d17", "Overfitting", "A model that memorizes its training set performs poorly on new data; "
     "regularization and more examples push it to generalize."),
    ("d18", "Convolutional networks", "Sharing small filters across an image detects the same "
     "feature anywhere, drastically cutting parameters versus dense layers."),
    ("d19", "Attention mechanism", "Instead of a fixed window, each position weighs every other "
     "position by relevance, letting a model relate distant tokens."),
    ("d20", "Word embeddings", "Mapping words to dense vectors places similar meanings near each "
     "other, so arithmetic on the vectors captures analogies."),
    ("d21", "Virtual memory", "The operating system gives each process its own address space, "
     "paging rarely used memory to disk and mapping pages on demand."),
    ("d22", "Process scheduling", "A scheduler decides which ready task runs next, balancing "
     "responsiveness for interactive jobs against throughput for batch work."),
    ("d23", "File systems", "Metadata structures track which disk blocks hold each file and keep "
     "directories, while journaling guards against corruption on crash."),
    ("d24", "Deadlock", "When processes each hold a resource the other needs and refuse to "
     "release, none can proceed; detection or ordered acquisition avoids it."),
    ("d25", "Memory allocation", "A heap allocator carves blocks from a large region and tracks "
     "free space, trading fragmentation against speed of reuse."),
    ("d26", "Garbage collection", "A managed runtime reclaims memory no longer reachable from "
     "the program, freeing developers from manual deallocation."),
    ("d27", "Type systems", "Checking the kinds of values a program manipulates before it runs "
     "catches whole classes of mistakes without a single test case."),
    ("d28", "Closures", "A function that captures variables from the scope where it was defined "
     "carries that environment with it wherever it is later called."),
    ("d29", "Recursion", "A routine that calls itself on a smaller input solves problems defined "
     "in terms of themselves, provided a base case stops the descent."),
    ("d30", "Concurrency primitives", "Locks, semaphores and channels coordinate threads so "
     "shared state is updated without races and work is handed off safely."),
    ("d31", "REST APIs", "Exposing resources behind predictable URLs and standard verbs lets "
     "clients and servers evolve independently over plain HTTP."),
    ("d32", "WebSockets", "Upgrading an HTTP connection to a persistent, two-way channel lets a "
     "server push updates without the client polling repeatedly."),
    ("d33", "Browser rendering", "The engine parses markup into a tree, computes styles and "
     "layout, then paints pixels, re-running the steps when the page changes."),
    ("d34", "Content delivery networks", "Copying assets to servers near users cuts distance and "
     "load on the origin, so pages load faster worldwide."),
    ("d35", "Cross-site scripting", "If a site renders attacker-supplied text as markup, injected "
     "script runs with the victim's session; escaping output prevents it."),
    ("d36", "Containers", "Packaging an application with its dependencies in an isolated "
     "namespace makes it run the same on a laptop and in production."),
    ("d37", "Message queues", "Placing work on a durable queue lets producers and consumers run "
     "at different rates and survive one side being temporarily down."),
    ("d38", "Idempotency", "An operation that can be applied many times with the same effect as "
     "once lets a client safely retry after an uncertain failure."),
    ("d39", "Eventual consistency", "Replicas that accept writes independently converge to the "
     "same state over time, trading immediate agreement for availability."),
    ("d40", "Rate limiting", "Capping how many requests a client may make in a window protects a "
     "service from overload and abusive traffic."),
]

# (id, text, {doc_id: grade}). Queries paraphrase the target to stress semantics.
QUERIES: list[tuple[str, str, dict[str, int]]] = [
    ("q01", "how does a database avoid scanning every page to find a record", {"d01": 2, "d04": 1}),
    ("q02", "recovering data after a crash by replaying changes", {"d02": 2, "d23": 1}),
    ("q03", "letting readers see a stable snapshot while writers proceed", {"d03": 2}),
    ("q04", "choosing the cheapest join order", {"d04": 2, "d01": 1}),
    ("q05", "spreading table rows across many machines", {"d05": 2, "d10": 1}),
    ("q06", "sharing a network link without overwhelming it", {"d06": 2, "d40": 1}),
    ("q07", "translating a hostname into a numeric address", {"d07": 2}),
    ("q08", "agreeing on encryption and verifying the server identity", {"d08": 2, "d13": 1}),
    ("q09", "reusing a response without asking the origin again", {"d09": 2, "d34": 1}),
    ("q10", "distributing connections over a pool of machines", {"d10": 2, "d05": 1}),
    ("q11", "encrypting so only the private key holder can read", {"d11": 2, "d15": 1}),
    ("q12", "a digest where collisions are infeasible", {"d12": 2}),
    ("q13", "proving a message was not tampered with and who sent it", {"d13": 2, "d11": 1}),
    ("q14", "encrypting fixed chunks under one shared key", {"d14": 2, "d11": 1}),
    ("q15", "deriving a shared secret over an open channel", {"d15": 2, "d11": 1}),
    ("q16", "nudging weights to reduce error during training", {"d16": 2, "d17": 1}),
    ("q17", "a model that memorizes and fails on unseen data", {"d17": 2, "d16": 1}),
    ("q18", "reusing small filters to spot a feature anywhere in a picture", {"d18": 2, "d19": 1}),
    ("q19", "weighing every token by relevance to relate distant words", {"d19": 2, "d20": 1}),
    ("q20", "dense vectors that place similar words together", {"d20": 2, "d19": 1}),
    ("q21", "giving each process its own address space and paging to disk", {"d21": 2, "d25": 1}),
    ("q22", "deciding which ready task runs next", {"d22": 2, "d24": 1}),
    ("q23", "tracking which disk blocks belong to each file", {"d23": 2, "d21": 1}),
    ("q24", "two processes stuck each holding what the other needs", {"d24": 2, "d30": 1}),
    ("q25", "reclaiming memory the program can no longer reach", {"d26": 2, "d25": 1}),
    ("q26", "catching type mistakes before the program runs", {"d27": 2}),
    ("q27", "a function that remembers variables from where it was created", {"d28": 2, "d29": 1}),
    ("q28", "coordinating threads so shared data has no races", {"d30": 2, "d24": 1}),
    ("q29", "pushing server updates over a persistent two-way channel", {"d32": 2, "d31": 1}),
    ("q30", "safely retrying an operation after an uncertain failure", {"d38": 2, "d39": 1}),
]


def main() -> None:
    here = Path(__file__).parent
    corpus_rows = [{"id": cid, "title": title, "text": text} for cid, title, text in CORPUS]
    query_rows = [{"id": qid, "text": text, "relevant": rel} for qid, text, rel in QUERIES]
    ch = write_jsonl(here / "retrieval_corpus.jsonl", "retrieval-corpus", "1.0.0", corpus_rows,
                     description="Hand-written short technical documents for retrieval evaluation.")
    qh = write_jsonl(here / "retrieval_queries.jsonl", "retrieval-queries", "1.0.0", query_rows,
                     description="Paraphrased queries with graded relevance labels (2=primary, "
                     "1=related).")
    print(f"corpus {len(corpus_rows)} docs sha256 {ch}")
    print(f"queries {len(query_rows)} sha256 {qh}")


if __name__ == "__main__":
    main()
