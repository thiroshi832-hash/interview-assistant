# Test interview script — for Oleksandr Yampolskyi's resume

Read these aloud one by one during a test interview to exercise different parts of the pipeline.
Numbered for reference; lines marked `[non-question]` should NOT trigger an answer.

Designed to test:
- regex-based question detection (clear openers, question marks)
- silence-net fallback (statements that imply a question)
- echo filter (long technical questions the candidate may rephrase)
- health scoring (mix of question lengths + follow-up density)
- resume-grounded answers (every question pulls from a specific resume detail)

---

## 1. Opening / warmup

1. "Hi Oleksandr, thanks for making the time today. How are you?"
2. "Let's get started. Walk me through your background in two or three minutes." *(tests: clear opener "walk me through")*
3. "Got it, that's helpful." *[non-question — should not fire]*
4. "And what are you looking for in your next role?"

---

## 2. CareMD — telehealth deep dive

5. "Tell me about The CareMD. What does the platform do, and what was your scope there?" *(clear opener)*
6. "You scaled to five thousand concurrent patient-doctor sessions. Walk me through the architecture that supported that." *(opener)*
7. "Why did you start with a React-plus-Flask monolith and only later introduce FastAPI async services? What changed?" *(why + ?)*
8. "Interesting." *[non-question]*
9. "How did you isolate real-time consultation workloads from the medical record systems? What were the actual coupling points you broke?" *(how + ?)*
10. "And what about HIPAA — how did compliance constrain the architecture decisions you just described?" *(silence-net target: "And what about X" — should still fire)*
11. "Right." *[non-question]*

---

## 3. The doctor-search performance story

12. "Tell me about the doctor search latency bottleneck. You went from four-thousand-millisecond p95 down to eighty-five milliseconds — what was the actual root cause?"
13. "Why materialized views and Elasticsearch specifically? Why not just better Postgres indexing?"
14. "What were the tradeoffs of running materialized views in production — staleness, refresh load, that kind of thing?"
15. "Okay, makes sense." *[non-question]*

---

## 4. AI / LLM features

16. "Let's talk about the AI features you built at CareMD. What problem were you actually solving with the OpenAI integration?" *(silence-net + opener test)*
17. "Walk me through the RAG system you prototyped for medical records search. Why LangChain plus a vector DB instead of just embedding plus Postgres-pgvector?"
18. "How did you handle PHI in the retrieval pipeline — what data did you actually pass to OpenAI versus keep inside your boundary?"
19. "What was the chunking strategy for medical records? Those documents have weird structure."
20. "If you were redoing the AI Knowledge Platform project today, what would you change?"

---

## 5. System design

21. "Imagine we're building a new symptom-triage feature that has to handle a sudden pandemic-scale traffic surge. How would you design it for low latency and high reliability while staying HIPAA-compliant?" *(long question; multi-clause — tests echo filter + question detector with preamble)*
22. "What would you monitor as your top three or four signals during a surge like that?"
23. "And how would you handle the case where the LLM provider has an outage in the middle of the surge?"

---

## 6. Comparent & Stripe

24. "Tell me about the Stripe webhook race condition you mentioned at Comparent. What was the failure mode and how did you make it idempotent?"
25. "You launched five hundred users in forty-eight hours with zero production incidents — what specifically did you do to de-risk that launch?"

---

## 7. Behavioral

26. "Describe a time when you had to push back on a product decision. What happened?" *(opener: describe)*
27. "Tell me about a time you mentored someone who was struggling. What did you change in your approach?"
28. "Have you ever shipped something you later regretted? What did you learn?" *(opener: "have you")*
29. "Give me an example of a technical disagreement with a teammate and how you resolved it."

---

## 8. Closing follow-ups

30. "We work mostly in Python and TypeScript with an event-driven microservice architecture on AWS. Anything in that stack you haven't used much yet?" *(non-obvious question — relies on silence-net or "anything")*
31. "What questions do you have for us?"
32. "Great, that's all from my side. Thanks for the conversation." *[non-question — should not fire]*

---

## Notes for running the test

- **Pause for 2–3 seconds** between each line so the VAD closes cleanly and the silence-net trigger has a chance to fire.
- For lines marked `[non-question]`, watch the answer panel — **no new answer should appear**.
- After question 16 ("Let's talk about…"), if no answer fires within ~3 seconds, the silence-net fallback is working as intended.
- After question 11 ("Right."), the post-answer cooldown should still be active — the silence net should NOT fire on a one-word ack.
- After your own spoken answer, the health bar should move toward "good" or "strong" if you spoke for 30–180 words.

## Expected behavior summary

| # | Trigger path | Should fire? |
|---|---|---|
| 2, 5, 6, 7, 9, 12, 13, 16, 17, 18, 21, 24, 26, 27, 28, 29 | Regex opener | Yes — immediate |
| 10, 14, 19, 20, 22, 23, 25, 30, 31 | Silence-net fallback (1.2 s after turn ends) | Yes — after delay |
| 3, 8, 11, 15, 32 | Non-question statement during cooldown | No |
| 4 | Short follow-up after cooldown ended (candidate spoke) | Yes |
