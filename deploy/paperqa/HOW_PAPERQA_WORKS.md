# Literature pipeline ownership

The native [literature server](https://github.com/liujs123456/aiscientist-mcp/tree/main/packages/literature) owns PDF parsing, local embeddings, corpus ranking, PaperQA answer synthesis and passage-level gene extraction. Its README describes bounded answers, citations, cost-aware pilots and complete result resources.

The application owns corpus version transfer, user specifications, selected-model credential guards, reviewed task routing, cancellation, cache identity and publication of verified tables. The same corpus must use matching embedding weights at index-build and query time. Curation reports retain negated, unsupported and review-flagged findings; a successful task or a retrieved passage is not itself a positive scientific claim.

See [deployment and maintenance](README.md) for the published runtime, local offline retrieval and separately enabled public lookup. Scientific operator commands now live in the native repository, without an app source mount.
