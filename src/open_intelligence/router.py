"""Decide which sources a request should be searched in.

The router routes by the *kind of artifact* the request needs, not by its subject. "Send Census data
to Snowflake with Python" is about data, but it needs code, so only GitHub is searched. "Fine-tune a
sentiment classifier" needs a pretrained model and a dataset (Hugging Face, Kaggle) and training code
(GitHub).

================  =================================================  ===========================
need              meaning                                            sources (primary first)
================  =================================================  ===========================
code              libraries, frameworks, SDKs, tools, apps           github
model             pretrained ML models / weights                     huggingface, kaggle
dataset           data to train, fine-tune or evaluate on            huggingface, kaggle
notebook          worked examples, competition solutions, EDA        kaggle
================  =================================================  ===========================

Predictive ML on the user's own data (churn, prices, fraud) is routed to code + notebook rather than
model: such models are trained from scratch with a library, so pretrained weights rarely help.

The calling agent understands the request far better than any word list, so it should decide the
needs itself and pass them (``decided_by: caller``). The keyword inference here is the fallback for
agents that don't, and a cross-check: when the text suggests a need the caller left out, the
decision says so in ``ambiguities``. It is deliberately simple and explainable: every routed need
lists the words that triggered it.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from open_intelligence.sources import NEED_DESCRIPTIONS, NEED_SOURCES, NEEDS, SOURCE_IDS, Need, SourceId


def _compile(patterns: Iterable[str]) -> list[re.Pattern[str]]:
    # (?<![\w-]) / (?![\w-]) are word boundaries that also treat "-" as part of a word.
    return [re.compile(rf"(?<![\w-])(?:{p})(?![\w-])", re.IGNORECASE) for p in patterns]


# Phrases that contain ML-looking words but are not about ML. They are blanked out before matching,
# so "dbt data model" does not route to model hubs and "type inference" is not ML inference.
_NOT_ML = _compile(
    [
        r"(?:data|dbt|domain|object|business|threat|pricing|role|view|3d|financial|mental|maturity|operating"
        r"|cost|security|permission|access|actor|relational|entity|semantic|database|orm|django|pydantic"
        r"|sqlalchemy|rails|active ?record|mongoose|prisma|sequelize|eloquent|dimensional|logical|physical"
        r"|conceptual|process|delivery|revenue|subscription|licensing|governance|programming|execution"
        r"|memory|concurrency|component|document|event|state|request|response|form|user|account|order"
        r"|product|star[- ]schema)[- ]models?",
        r"models?[- ](?:view|controller|class(?:es)?|layer|fields?|serializers?|migrations?|relationships?)",
        r"mvc|mvvm",
        r"type[- ]inference",
        r"data[- ]classification|security[- ]classification|classified[- ]ads?",
        r"(?:linux|os|operating[- ]system)[- ]kernels?",
    ]
)

# Enough on their own to mean "this needs an ML model".
_ML_STRONG = _compile(
    [
        r"machine[- ]learning",
        r"deep[- ]learning",
        r"neural[- ]net(?:work)?s?",
        r"llms?",
        r"(?:large[- ])?language[- ]models?",
        r"foundation[- ]models?",
        r"fine[- ]?tun(?:e|es|ed|ing)",
        r"pre[- ]?trained",
        r"embeddings|embedding[- ]models?",
        r"semantic[- ]search",
        r"sentiment",
        r"named[- ]entit(?:y|ies)|ner",
        r"object[- ]detection",
        r"(?:image|semantic|instance)[- ]segmentation",
        r"speech[- ]recognition|speech[- ]to[- ]text|asr",
        r"text[- ]to[- ]speech|tts|voice[- ]clon(?:e|ing)",
        r"transcri(?:be|bes|bing|ption|ptions)",
        r"ocr",
        r"image[- ]generation|generat(?:e|ing)[- ]images?|text[- ]to[- ](?:image|video)",
        r"(?:stable[- ])?diffusion[- ]models?|stable[- ]diffusion",
        r"computer[- ]vision",
        r"nlp|natural[- ]language[- ]processing",
        r"rag|retrieval[- ]augmented(?:[- ]generation)?",
        r"vision[- ]language|vlms?|multimodal",
        r"q?lora|quanti[sz](?:e|ed|ation)|gguf",
        r"pytorch|tensorflow|keras",
        r"zero[- ]shot|few[- ]shot",
        r"chat[- ]?bots?",
        r"classif(?:y|ies|ier|iers|ication)",
        r"train(?:ing)?\s+(?:a|an|the|my|our|your)?\s*(?:custom\s+)?(?:model|classifier|network|llm)s?",
        r"model[- ]training|model[- ]weights",
        r"(?:face|image|handwriting|speech|voice|emotion|intent)[- ]recognition",
        r"recommend(?:er|ation)[- ](?:system|engine|model)s?",
        r"question[- ]answering",
        r"machine[- ]translation",
        r"whisper|yolo(?:v?\d+)?|bert|gpt(?:-?\d(?:\.\d)?)?|llama|mistral|gemma|qwen",
        r"ai[- ]models?",
    ]
)

# Predictive ML on the user's own (usually tabular) data: churn, prices, demand, fraud. The model is
# normally trained from scratch with a library, so pretrained weights help less than ML libraries
# (code) and worked solutions on similar data (notebooks).
_ML_PREDICTIVE = _compile(
    [
        r"predict(?:s|ing|ion|ions|ive)?",
        r"regression",
        r"time[- ]series[- ]forecast(?:ing)?|forecasting[- ]models?|demand[- ]forecast(?:ing)?",
        r"anomaly[- ]detection|fraud[- ]detection|churn",
        r"scikit[- ]learn|sklearn|xgboost|lightgbm|catboost|automl|tabular",
    ]
)

# Suggest ML, but only count when there is other ML evidence (or two of them).
_ML_WEAK = _compile(
    [
        r"forecast(?:s|ing)?",
        r"detect(?:s|ing|ion)?",
        r"recogni[sz](?:e|es|ing|tion)",
        r"recommend(?:s|ing|ations?)?",
        r"summari[sz](?:e|es|ing|ation)",
        r"translat(?:e|es|ing|ion)",
        r"ai|artificial[- ]intelligence|ml",
        r"inference",
        r"vector[- ](?:search|database|store)",
        r"time[- ]series",
    ]
)

# The bare noun. Counts as an ML model only next to ML evidence (it is often a data/ORM model).
_MODEL_NOUN = _compile([r"models?|checkpoints?"])

_DATASET = _compile(
    [
        r"datasets?|data[- ]sets?",
        r"training[- ](?:data|set|corpus|examples)",
        r"(?:test|validation|eval(?:uation)?|dev|holdout)[- ](?:sets?|splits?|data)",
        r"label(?:l)?ed[- ](?:data|examples|images|samples|text)",
        r"annotat(?:ed|ion|ions)",
        r"corp(?:us|ora)",
        r"benchmark[- ](?:datasets?|suites?)",
        r"ground[- ]truth",
        r"sample[- ]data|synthetic[- ]data|open[- ]data|public[- ]data",
    ]
)

# Training a model implies it needs data (dataset) and training code (code).
_TRAINING = _compile(
    [r"train(?:s|ed|ing)?|re-?train(?:ing)?", r"fine[- ]?tun(?:e|es|ed|ing)", r"distill(?:ation)?"]
)

_NOTEBOOK = _compile(
    [
        r"notebooks?|jupyter|colab",
        r"competitions?|leaderboards?",
        r"eda|exploratory[- ]data[- ]analysis",
        r"baselines?|starter[- ](?:code|notebooks?)",
        r"winning[- ]solutions?|solution[- ]write[- ]?ups?",
    ]
)

_CODE = _compile(
    [
        r"build(?:s|ing)?",
        r"creat(?:e|es|ing)",
        r"writ(?:e|es|ing)",
        r"implement(?:s|ing|ation)?",
        r"develop(?:s|ing|ment)?",
        r"automat(?:e|es|ing|ion)",
        r"scripts?",
        r"apps?|applications?",
        r"services?|micro-?services?",
        r"apis?|sdks?|clis?",
        r"librar(?:y|ies)|packages?|frameworks?|tools?|toolkits?",
        r"pipelines?|etl|elt|reverse[- ]etl|orchestrat(?:e|ion)|sql|dbt",
        r"integrat(?:e|es|ing|ion|ions)|connectors?|sync(?:ing)?",
        r"backend|frontend|full[- ]?stack|websites?|web[- ]?apps?",
        r"bots?|plugins?|extensions?",
        r"deploy(?:s|ing|ment)?|serv(?:e|ing)|server",
        r"dashboards?|ui",
        r"projects?|repo(?:sitory|sitories|s)?|code(?:base)?",
        r"python|javascript|typescript|node(?:\.js)?|golang|rust|java|kotlin|swift|c\+\+|c#|ruby|php|scala|dart|flutter|react",
    ]
)

_MENTION: dict[SourceId, list[re.Pattern[str]]] = {
    "github": _compile([r"git[- ]?hub"]),
    "huggingface": _compile([r"hugging[- ]?face", r"hf[- ]hub", r"hf"]),
    "kaggle": _compile([r"kaggle"]),
}
_NEGATION = re.compile(
    r"(?<![\w-])(?:no|not|without|except|excluding|exclude|skip|avoid|don'?t\s+(?:use|search|need|want))"
    r"(?:\s+\w+){0,3}?\s+(git[- ]?hub|hugging[- ]?face|kaggle)(?![\w-])",
    re.IGNORECASE,
)
_MENTION_NEEDS: dict[SourceId, tuple[Need, ...]] = {
    "github": ("code",),
    "huggingface": ("model", "dataset"),
    "kaggle": ("dataset", "notebook"),
}


@dataclass
class NeedDecision:
    need: Need
    evidence: list[str]
    implied_by: str | None = None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "need": self.need,
            "meaning": NEED_DESCRIPTIONS[self.need],
            "evidence": self.evidence,
        }
        if self.implied_by:
            out["implied_by"] = self.implied_by
        return out


@dataclass
class SourcePlan:
    source: SourceId
    needs: list[Need]
    role: Literal["primary", "secondary"]
    why: str


@dataclass
class RoutingDecision:
    needs: list[NeedDecision]
    sources: list[SourcePlan]
    skipped: list[dict[str, str]]
    confidence: Literal["high", "medium", "low"]
    decided_by: Literal["caller", "inferred"]
    # Things that could change the routing if the router read them wrong: worth a second look.
    ambiguities: list[str] = field(default_factory=list)
    # How a word was read, for transparency; these do not lower confidence.
    notes: list[str] = field(default_factory=list)

    @property
    def need_names(self) -> list[Need]:
        return [n.need for n in self.needs]

    @property
    def source_ids(self) -> list[SourceId]:
        return [s.source for s in self.sources]

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "needs": [n.as_dict() for n in self.needs],
            "sources": [s.__dict__ for s in self.sources],
            "skipped_sources": self.skipped,
            "confidence": self.confidence,
            "decided_by": self.decided_by,
            "ambiguities": self.ambiguities,
        }
        if self.notes:
            out["notes"] = self.notes
        return out


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("_", " ")).strip()


def _hits(patterns: Sequence[re.Pattern[str]], text: str) -> list[str]:
    seen: dict[str, None] = {}
    for p in patterns:
        for m in p.finditer(text):
            seen.setdefault(m.group(0).lower(), None)
    return list(seen)


def _blank(patterns: Sequence[re.Pattern[str]], text: str) -> tuple[str, list[str]]:
    removed: list[str] = []

    def sub(m: re.Match[str]) -> str:
        removed.append(m.group(0).lower())
        return " " * len(m.group(0))

    for p in patterns:
        text = p.sub(sub, text)
    return text, removed


@dataclass
class _Inference:
    needs: dict[Need, NeedDecision]
    excluded: dict[SourceId, str]
    mentioned: list[SourceId]
    ambiguities: list[str]
    notes: list[str]
    weak_only: bool


def _infer(task: str) -> _Inference:
    raw = _normalize(task)
    text, neutralized = _blank(_NOT_ML, raw)
    needs: dict[Need, NeedDecision] = {}
    ambiguities: list[str] = []

    excluded: dict[SourceId, str] = {}
    for m in _NEGATION.finditer(raw):
        word = m.group(1).lower().replace(" ", "").replace("-", "")
        sid: SourceId = "github" if word == "github" else "huggingface" if word == "huggingface" else "kaggle"
        excluded[sid] = f"excluded by the request ({m.group(0).strip()!r})"
    mentioned = [sid for sid, pats in _MENTION.items() if sid not in excluded and _hits(pats, raw)]

    strong = _hits(_ML_STRONG, text)
    predictive = _hits(_ML_PREDICTIVE, text)
    weak = _hits(_ML_WEAK, text)
    nouns = _hits(_MODEL_NOUN, text)
    data = _hits(_DATASET, text)
    code = _hits(_CODE, text)
    weak_only = False
    ml = strong or len(weak) >= 2 or (nouns and weak)
    if ml and data and not nouns and not code and not _hits(_TRAINING, text):
        # "Find a German NER dataset": the ML words describe the data being looked for.
        ambiguities.append(
            f"ML terms ({', '.join((strong + weak)[:3])}) are read as describing the dataset. "
            "Pass needs=['dataset', 'model'] if a pretrained model is also needed."
        )
    elif ml:
        needs["model"] = NeedDecision("model", strong + predictive + weak + nouns)
        weak_only = not strong
    elif predictive:
        why = "predictive ML on your own data is usually trained from scratch with a library"
        needs["code"] = NeedDecision("code", predictive, implied_by=why)
        needs["notebook"] = NeedDecision(
            "notebook", predictive, implied_by=why + "; worked solutions show how"
        )
        ambiguities.append(
            f"{predictive[0]!r} reads as predictive ML you train on your own data, so ML libraries and worked "
            "notebooks are searched instead of pretrained-model hubs. Pass needs=['model', ...] for pretrained models."
        )
    elif weak:
        ambiguities.append(
            f"{weak[0]!r} can mean an ML model, but nothing else in the request points to one, so model hubs "
            "are not searched. Pass needs=['model', ...] if an ML model is meant."
        )
    elif nouns:
        ambiguities.append(
            f"The request says {nouns[0]!r} without any ML context, so it is read as a software/data model, not an "
            "ML model. Pass needs=['model', ...] if a pretrained ML model is meant."
        )
    notes = [
        f"{phrase!r} is read as a software/data concept, not ML." for phrase in dict.fromkeys(neutralized)
    ]

    training = _hits(_TRAINING, text) if "model" in needs else []
    if data:
        needs["dataset"] = NeedDecision("dataset", data)
    elif training:
        needs["dataset"] = NeedDecision("dataset", training, implied_by="training or fine-tuning needs data")

    notebook = _hits(_NOTEBOOK, text)
    if notebook:
        needs["notebook"] = NeedDecision("notebook", notebook)

    if code:
        needs["code"] = NeedDecision("code", code)
    elif training and "code" not in needs:
        needs["code"] = NeedDecision(
            "code", training, implied_by="training or fine-tuning needs training code"
        )

    for sid in mentioned:
        if not any(sid in NEED_SOURCES[n] for n in needs):
            for need in _MENTION_NEEDS[sid]:
                needs.setdefault(need, NeedDecision(need, [sid], implied_by=f"the request names {sid}"))

    return _Inference(needs, excluded, mentioned, ambiguities, notes, weak_only)


_SKIP_REASON: dict[SourceId, str] = {
    "github": "no code need: nothing to build on was detected",
    "huggingface": "no model or dataset need",
    "kaggle": "no dataset, notebook or model need",
}


def route(task: str, needs: Sequence[str] | None = None) -> RoutingDecision:
    """Route ``task`` to sources. ``needs``, when given, is the caller's decision and wins."""
    inferred = _infer(task)
    ambiguities = list(inferred.ambiguities)

    if needs:
        unknown = [n for n in needs if n not in NEEDS]
        if unknown:
            raise ValueError(f"unknown needs {unknown}; allowed: {list(NEEDS)}")
        chosen: dict[Need, NeedDecision] = {
            n: NeedDecision(n, ["chosen by the caller"]) for n in NEEDS if n in needs
        }
        # The caller's choice is not second-guessed by our word lists; we only point out needs the
        # text suggests that the caller left out. Explicit exclusions ("not on Kaggle") still apply.
        ambiguities = [
            f"The request text also suggests {n!r} ({', '.join(d.evidence[:3])}); add it to needs if it applies."
            for n, d in inferred.needs.items()
            if n not in chosen
        ]
        decided_by: Literal["caller", "inferred"] = "caller"
        confidence: Literal["high", "medium", "low"] = "high"
    else:
        chosen = dict(inferred.needs)
        decided_by = "inferred"
        if not chosen:
            chosen["code"] = NeedDecision("code", [], implied_by="no clear signal; defaulting to code")
            ambiguities.append("No clear signal in the request; defaulted to searching code only.")
            confidence = "low"
        elif inferred.weak_only or ambiguities:
            confidence = "medium"
        else:
            confidence = "high"

    ordered = [chosen[n] for n in NEEDS if n in chosen]
    plans: dict[SourceId, SourcePlan] = {}
    for decision in ordered:
        for rank, sid in enumerate(NEED_SOURCES[decision.need]):
            if sid in inferred.excluded:
                continue
            role: Literal["primary", "secondary"] = "primary" if rank == 0 else "secondary"
            plan = plans.get(sid)
            if plan is None:
                plans[sid] = SourcePlan(sid, [decision.need], role, "")
            else:
                plan.needs.append(decision.need)
                if role == "primary":
                    plan.role = "primary"
    for plan in plans.values():
        plan.why = "; ".join(f"{n}: {NEED_DESCRIPTIONS[n]}" for n in plan.needs)

    sources = [plans[sid] for sid in SOURCE_IDS if sid in plans]
    skipped = [
        {"source": sid, "why": inferred.excluded.get(sid, _SKIP_REASON[sid])}
        for sid in SOURCE_IDS
        if sid not in plans
    ]
    if not sources:
        ambiguities.append("Every source that serves these needs was excluded by the request.")
    return RoutingDecision(ordered, sources, skipped, confidence, decided_by, ambiguities, inferred.notes)
