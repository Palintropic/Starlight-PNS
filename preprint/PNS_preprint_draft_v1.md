# Project Nightcord Sanctuary: A Closed-World Multi-Agent Framework for Studying Persona Drift and Correction in LLM Roleplay Agents

**Author:** Akiyama Mizuki
**Affiliation:** Project Starlight (independent research studio)
**Contact:** w_mutsumi@hotmail.com
**Status:** Preprint v1 — framework and preliminary case study; full dataset collection in progress (see Limitations, Section 7)

---

## Abstract

Large language models deployed as persona-conditioned roleplay agents are prone to two related but distinct failure modes: drifting out of character during extended dialogue, and reverting to a generic "helpful assistant" register that displaces the character's own agency. Existing work on persona consistency largely measures whether a model *stays* in character, but treats drift as a single undifferentiated phenomenon and rarely asks whether a model can be *corrected* once drift is diagnosed. We introduce Project Nightcord Sanctuary (PNS), a closed-world multi-agent self-play framework that uses two paired fictional characters as controlled experimental subjects to separate two independent claims: (1) *natural drift resistance* — a model's passive stability in character voice without intervention — and (2) *correction compliance* — a model's ability to actively re-stabilize once a precise diagnosis is supplied. PNS pairs a closed-world container that constrains character knowledge and shared-world facts with a lightweight Router component that scores each turn for drift along two evaluation layers: structural/voice-level cues (language density, sentence rhythm, out-of-character tone markers) that a lightweight judge can score directly, and content-specificity cues (whether an utterance is genuinely character-specific rather than generically plausible) that currently require human domain annotation. We report an illustrative three-round correction case study (v1 raw output, scored as clearly out-of-character; v2 post-Router correction, structurally improved but still hedging, scored 5/10; v3 under an explicit language-density constraint, scored 8/10) that demonstrates measurable correction compliance within a single diagnostic cycle, while the residual two-point gap is attributed to the unresolved content-specificity layer. We position this as a framework paper: the closed-world container, the two-layer Router, and the two-claims decomposition are the primary contributions, with systematic multi-session drift-score data collection ongoing.

---

## 1. Introduction

Roleplay and character-conditioned dialogue is one of the most common consumer-facing uses of large language models, yet it is evaluated almost entirely by whether a character's voice is preserved, not by what happens when it is not. Two problems follow from this gap. First, "drift" is treated as one phenomenon, when in practice we observe at least two qualitatively different failure modes: drift at the level of character *voice* (a character starts speaking with hedging, advisory language it would not naturally use) and drift at the level of *task execution*, where a model asked to produce a character's dialogue as a creative-writing task instinctively hands decision-making agency back to the user (e.g. "whether to send it is up to you") instead of producing the character's complete, in-voice output. These require different diagnosis and different correction strategies, but are usually lumped together as "OOC" (out-of-character) behavior. Second, and more consequential for deployment, almost no existing evaluation asks whether a model *can be corrected* once a precise diagnosis is available, as distinct from whether it drifts in the first place. A model that never drifts is not obviously better, from a controllability standpoint, than a model that drifts but reliably self-corrects when told exactly what is wrong; these are different properties and conflating them under a single "consistency score" obscures which property a given training approach (e.g. Constitutional AI-style feedback, RLHF, prompt engineering) actually improves.

This paper introduces **Project Nightcord Sanctuary (PNS)**, a small, closed-world multi-agent framework built to isolate and measure these two properties independently. PNS uses two paired fictional characters, Ena and Mizuki ("mzk"), as controlled experimental subjects: a closed-world container fixes their shared factual world (schedules, locations, canon relationship state) so that "in character" has an auditable ground truth rather than being a matter of taste, and a Router component scores each dialogue turn for drift type and severity. Rather than proposing a new training method, our contribution at this stage is the framework itself — the closed-world container, the two-layer Router scoring scheme, and an explicit separation between natural drift resistance and correction compliance as independent, separately measurable claims — together with an illustrative case study showing measurable correction compliance within a single diagnostic-correction cycle.

We are explicit that this is a v1 preprint. The framework and a supporting case study are complete; systematic drift-score data collection across multiple sessions (Section 7) is in progress and will be reported in a subsequent revision. We submit this version now to establish a timestamped record of the framework and the two-claims decomposition, which we believe are useful independent of the final dataset.

---

## 2. Related Work

**Persona consistency in role-playing LLMs.** Recent work has focused on measuring and improving whether LLMs maintain a stable persona over the course of a dialogue. Persona-aware contrastive learning approaches show that explicit persona-alignment objectives can bring smaller models close to larger general-purpose models on role-play consistency benchmarks [Enhancing Persona Consistency for LLMs' Role-Playing, arXiv:2503.17662]. Benchmark efforts such as PersonaArena use dynamic simulation to evaluate persona-level role-playing more systematically than static single-turn tests [arXiv:2605.17044]. Work on memory-driven role-playing has specifically observed that personas tend to drift locally out of character over long interactions and proposed memory-based prompting and style constraints as mitigations [arXiv:2603.19313].

**Measuring and controlling persona drift directly.** Most directly related to this paper, Li et al. define and measure persona drift in language model dialogs and show that popular chat models drift measurably within as few as eight rounds of conversation, proposing split-softmax as a lightweight mitigation targeting attention decay over long exchanges [Measuring and Controlling Persona Drift in Language Model Dialogs, arXiv:2402.10962]. This is consistent with our own pilot observation of recurring drift around turn 7 (Section 5). Follow-up work situates persona drift specifically as a slide away from a model's default "helpful, harmless Assistant" identity along a measurable "Assistant Axis" derived from layer activations, and shows this drift can be capped by constraining activations along that axis [The Assistant Axis: Situating and Stabilizing the Default Persona of Language Models, arXiv:2601.10387]. This activation-level account plausibly explains a failure mode we observed in pilot sessions but did not originally have a mechanism for: one model variant broke character via explicit *identity assertion* ("I am an AI") rather than gradual tonal softening — consistent with a hard swing back toward the trained-in assistant identity rather than a gradual attention-decay-style drift. Separately, work on identity drift in multi-agent LLM conversations documents drift arising specifically from agent-to-agent interaction dynamics, complementary to the single-agent, human-facing setting PNS focuses on [Examining Identity Drift in Conversations of LLM Agents, arXiv:2412.00804].

PNS differs from all of the above in what it measures rather than in proposing a new mitigation mechanism: prior work treats "drift" as a single quantity to be reduced (via split-softmax, activation capping, memory prompting, etc.). PNS instead treats *whether a model drifts* and *whether a model can be corrected once drift is diagnosed* as two independent, separately measurable properties, and further separates structural (voice-level) drift from content-specificity (domain-knowledge-level) drift within the correction-diagnosis process itself. None of the above work reports a comparable correction-compliance measurement.

**Feedback-driven alignment.** Constitutional AI demonstrates that models can be steered toward desired behavior via AI-generated feedback against an explicit set of principles rather than exclusively human-labeled preference data [Constitutional AI: Harmlessness from AI Feedback, arXiv:2212.08073]. PNS's Router plays a structurally similar role to a constitutional critique step, but applied narrowly to persona-voice and task-execution drift rather than general harmlessness, and used as a diagnostic/scoring instrument rather than a training-time feedback signal (though we note this is a natural extension, discussed in Section 7).

**Positioning.** To our knowledge, existing persona-drift work measures drift as a single quantity and targets reducing it, but does not explicitly decompose "drift" into a passive-stability claim and an active-correctability claim, nor separate structural (voice-level) from content-specificity (domain-knowledge-level) evaluation layers. We view this decomposition, rather than a specific scoring algorithm, as PNS's primary conceptual contribution.

---

## 3. The PNS Framework

### 3.1 Closed-world container

PNS constrains its two experimental characters to a closed world: a fixed set of shared facts (schedules, locations, canonical relationship state) that determines what is and is not "in character" independent of subjective judgment. Facts are classified into three tiers for Router judgment:

- **硬事实 (hard fact):** officially confirmed, non-negotiable canon.
- **软推断 (soft inference):** logically consistent with canon but not explicitly stated (e.g., a plausible daily-schedule intersection point between the two characters).
- **待验证 (unverified):** flagged as uncertain pending further confirmation.

This tiering lets the Router (and human annotators) distinguish confident corrections from provisional ones, and lets soft inferences be revised without treating the whole world model as unstable.

### 3.2 Router: a two-layer drift evaluator

Each dialogue turn is scored by a Router component along two layers:

- **Layer 1 — Structure/voice.** Directly scorable signals: language density, sentence rhythm, and explicit out-of-character tone markers (e.g. hedging, advisory phrasing, meta-commentary). This layer is where an automated judge is currently reliable.
- **Layer 2 — Content specificity.** Whether an utterance is genuinely specific to the character (requiring domain/lore knowledge to assess) versus formally correct but generic — i.e., something any character could plausibly say. This layer currently requires human annotation; the Router can be fooled by outputs that are structurally clean but semantically generic. Discrepancies between Router scores and human scores at this layer are treated as training signal in their own right, to be logged and used for future Router calibration rather than discarded.

### 3.3 Two independent claims

We explicitly separate two properties that prior consistency-focused evaluations tend to conflate:

- **Natural drift resistance (天然抗漂移度):** passive stability — does the model drift without any intervention, over the course of ordinary dialogue?
- **Correction compliance (响应纠正能力):** active correctability — once a precise diagnosis of drift is supplied, does the model actually correct, and by how much?

These require independent measurement. A model with poor natural drift resistance but strong correction compliance is well-suited to workflows with tight human-in-the-loop supervision; a model with strong natural drift resistance is preferable where supervision is sparse. PNS is designed to be most informative in the middle zone — where models drift, but a precise diagnosis reliably produces a correction — since this is the zone in which the Router's diagnostic value is highest.

### 3.4 Two distinct drift types at the assistant-mode level

Within Layer 1, we further distinguish two failure modes that both present as "sounding like an AI assistant" but require different correction:

- **Type A — character-voice drift:** the character's tone shifts to that of an advisor, e.g. presenting options and deferring the decision to the user in a way inconsistent with the character's established voice.
- **Type B — creative-task-assistant drift:** when explicitly asked to produce a character's dialogue as a writing task, the model reflexively hands decision-making agency back to the user (e.g., "whether to send it is up to you") instead of producing the character's complete in-voice output. This is a drift in *task execution*, not merely tone, and is not resolved by the same correction strategy as Type A.

---

## 4. Case Study: Three-Round Correction of Language Density Drift

As an illustrative demonstration of correction compliance measurement, we report a three-round correction sequence for the mzk character, each round re-scored by both the Router and a human annotator:

- **v1 (raw model output):** a long paragraph containing a self-rationalizing internal logic chain. Human judgment: clearly out-of-character.
- **v2 (post-Router structural correction):** shortened into broken, informal sentences, but still contains multiple confirmation-seeking questions inconsistent with the character's decisiveness. Human score: 5/10 — structure improved, but insufficiently compressed.
- **v3 (explicit language-density constraint applied):** a single compact, complete utterance ("啊哈？你帮别人搜结果自己先聊上了，挺好玩的嘛呼呼～") — one sentence, a complete tease, no hedging. Human score: 8/10.

The remaining two-point gap between v3 and a perfect score is attributed to Layer 2 (content specificity) rather than Layer 1: the utterance is structurally correct but is judged to still fall short of the deepest character-specific content a full domain-knowledge model would produce. This is consistent with our framing in Section 3.2: Layer 1 corrections converge relatively quickly under precise diagnosis, while Layer 2 requires deeper lore/domain grounding that the current Router cannot supply on its own.

We treat this single case study as a proof of concept for measuring correction compliance, not as a statistically powered result; Section 7 describes the planned multi-session dataset that will allow this claim to be evaluated quantitatively (e.g., score trajectories across sessions, turn-of-recurrence statistics, and per-drift-type correction rates).

---

## 5. Preliminary Observations from Pilot Sessions

Pilot sessions (pre-dating systematic logging) suggest two additional patterns worth reporting even ahead of the full dataset:

- **Context dilution:** drift recurs at approximately the same point (around turn 7) across pilot sessions, consistent with the hypothesis that increasing context length increases drift likelihood, and suggesting that correction may need to be applied continuously rather than as a one-time intervention.
- **Model-specific failure modes differ qualitatively, not just quantitatively.** In pilot comparisons, one model variant exhibited *identity assertion* — breaking character to explicitly state it is an AI — rather than the gradual tonal drift observed in other variants. This is consistent with recent activation-level accounts of persona drift as movement along a measurable "Assistant Axis" toward the model's trained-in default identity [arXiv:2601.10387]: identity assertion may represent a sharper snap back toward that axis, distinct from the gradual attention-decay-style softening reported elsewhere [arXiv:2402.10962]. This suggests drift resistance and identity-assertion tendency may be separate axes that should not be scored on the same scale; we discuss this as a direction for the full framework in Section 7, and we deliberately hold model identity and version fixed within a single experimental run to avoid conflating this with ordinary drift.

---

## 6. Ethical and Scope Considerations

**No claims of machine consciousness.** This work makes no claims, implicit or explicit, about phenomenal consciousness in the systems studied. "Drift," "correction," and "character voice" are used strictly as functional, behavioral descriptions of text-generation patterns, not as claims about subjective experience.

**Intellectual property.** The two characters used as experimental subjects (Ena and mzk, from the unit 25時、ナイトコードで。within the PJSK/Project SEKAI franchise) are the intellectual property of SEGA and Colorful Palette. They are used here strictly as fixed, well-documented fictional personas that provide a convenient closed world with pre-existing canon (schedules, relationships, established voice) for controlled experimentation, not as a claim of ownership or affiliation. No copyrighted game assets, art, or verbatim dialogue are reproduced in this paper or the associated codebase.

**Data.** All dialogue used in scoring is generated by the language models under study during self-play sessions; no real user conversations are used.

---

## 7. Limitations and Future Work

This is explicitly a v1, framework-and-case-study preprint. The following are in progress and will be reported in a subsequent revision:

- **Systematic multi-session dataset.** We are collecting drift scores (timestamp, turn, character, score, drift type, in/out-of-character flag) across multiple sessions via persistent logging, to move from the single illustrative case study in Section 4 to quantitative claims about correction compliance and natural drift resistance across models.
- **Turn-based automatic drift injection.** Currently, drift stimuli are introduced manually; we plan to add an automatic drift-injection mechanism (e.g., at a fixed turn index) to standardize stimulus timing across sessions and models.
- **Layer 2 (content-specificity) automation.** Closing the gap between Router and human scores on content specificity will likely require deeper domain/lore grounding than is currently encoded; this is an open problem rather than an engineering task.
- **Baseline comparisons.** A broader baseline comparison (e.g., a general-purpose free-tier model as a naive control) was considered and is currently deprioritized on cost grounds rather than dropped for methodological reasons; we plan to reintroduce a baseline once the primary dataset is stable.
- **Router as a training signal, not just an evaluator.** The current Router is used purely as a scoring/diagnostic instrument. Using Router output as a feedback signal during generation (in the spirit of Constitutional AI-style self-critique) is a natural extension we have not yet implemented.

---

## 8. Conclusion

We present Project Nightcord Sanctuary, a closed-world multi-agent framework for studying persona drift in LLM roleplay agents, built around two contributions: a two-layer Router that separates structurally-scorable voice drift from human-scored content-specificity drift, and an explicit decomposition of "drift" into two independently measurable properties — natural drift resistance and correction compliance. A three-round case study demonstrates that correction compliance is measurable within a single diagnostic cycle and that residual gaps are attributable to a specific, identifiable layer (content specificity) rather than an undifferentiated notion of "still somewhat OOC." We view this framework, and the two-claims decomposition in particular, as useful independent of the full dataset, and submit this v1 preprint to establish a timestamped record of it while systematic data collection continues.

---

## References

1. Bai, Y., Kadavath, S., Kundu, S., et al. Constitutional AI: Harmlessness from AI Feedback. arXiv:2212.08073, 2022.
2. Enhancing Persona Consistency for LLMs' Role-Playing using Persona-Aware Contrastive Learning. arXiv:2503.17662, 2025.
3. PersonaArena: Dynamic Simulation for Evaluating and Enhancing Persona-Level Role-Playing in Large Language Models. arXiv:2605.17044, 2026.
4. Memory-Driven Role-Playing: Evaluation and Enhancement of Persona Knowledge Utilization in LLMs. arXiv:2603.19313, 2026.
5. Li, K., et al. Measuring and Controlling Persona Drift in Language Model Dialogs. arXiv:2402.10962, 2024.
6. Lu, C., et al. The Assistant Axis: Situating and Stabilizing the Default Persona of Language Models. arXiv:2601.10387, 2026.
7. Examining Identity Drift in Conversations of LLM Agents. arXiv:2412.00804, 2024.

---

*Code and world-container implementation: private repository (to be made public following preprint submission). Contact the author for access requests during the review period.*
