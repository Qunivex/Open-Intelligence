"""One adapter per (source, need): which upstream calls to make and how to read their results.

Everything that knows what an upstream MCP server's output looks like lives here. The formats
were recorded from the live Hugging Face and Kaggle servers on 2026-11-13 (see tests/fixtures).
GitHub's follow github/github-mcp-server's documented result types and have NOT been checked
against the live server yet, because that needs a token.

Parsers are tolerant: a field that is missing or unreadable stays unknown; it never raises.
Hugging Face's search and details tools answer in Markdown meant for a model to read, so those
two parsers are the most likely to drift. ``pytest -m live`` checks them against the real server.
"""

from __future__ import annotations

import html
import json
import re
from datetime import UTC, datetime
from typing import Any

from open_intelligence.connector import CallOutcome
from open_intelligence.ranking.models import Call, Candidate, DatasetStructure, EvalResult
from open_intelligence.sources import Need

DOC_CHARS = 8000  # how much README / card text is kept for matching


# --- small readers --------------------------------------------------------------------------------

_SCALE = {"k": 1e3, "m": 1e6, "b": 1e9}


def parse_number(text: Any) -> float | None:
    """'4.7K' -> 4700.0, '121.6K', '3.4M', '1,234', 52."""
    if isinstance(text, bool):
        return None
    if isinstance(text, int | float):
        return float(text)
    if not isinstance(text, str):
        return None
    m = re.fullmatch(r"\s*([0-9][0-9,]*\.?[0-9]*)\s*([kKmMbB]?)\s*", text)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", "")) * _SCALE.get(m.group(2).lower(), 1.0)
    except ValueError:
        return None


def parse_date(text: Any) -> datetime | None:
    """ISO timestamps (any fraction length, 'Z') and Hugging Face's '12 Nov, 2023'."""
    if not isinstance(text, str) or not text.strip():
        return None
    text = text.strip()
    iso = re.sub(r"(\.\d{6})\d+", r"\1", text).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(iso)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except ValueError:
        pass
    for fmt in ("%d %b, %Y", "%d %B, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def _json(text: str) -> Any:
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None


def _str(value: Any) -> str:
    return html.unescape(value).strip() if isinstance(value, str) else ""


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _set_metric(candidate: Candidate, name: str, value: Any) -> None:
    number = parse_number(value)
    if number is not None:
        candidate.metrics[name] = number


class Adapter:
    """Base: what the pipeline asks of every (source, need)."""

    source: str = ""
    need: Need = "code"
    reads_shortlist: bool = True  # False when the search result already carries the full card

    def search_calls(self, queries: list[str], limit: int, languages: list[str] | None = None) -> list[Call]:
        raise NotImplementedError

    def parse_search(self, outcome: CallOutcome, limit: int) -> list[Candidate]:
        raise NotImplementedError

    def enrich_calls(self, candidates: list[Candidate]) -> list[Call]:
        return []

    def follow_up_calls(self, candidates: list[Candidate]) -> list[Call]:
        """Calls that can only be planned once ``enrich_calls`` have been applied."""
        return []

    def apply(self, candidates: dict[str, Candidate], call: Call, outcome: CallOutcome) -> None:
        return None

    def _new(self, id_: str, url: str) -> Candidate:
        return Candidate(source=self.source, need=self.need, id=id_, url=url, owner=id_.split("/", 1)[0])


# --- GitHub ---------------------------------------------------------------------------------------


class GitHubCode(Adapter):
    source = "github"
    need: Need = "code"

    def search_calls(self, queries: list[str], limit: int, languages: list[str] | None = None) -> list[Call]:
        calls = []
        for i, query in enumerate(queries):
            # minimal_output=false returns full repository objects (license, pushed_at).
            base = {"query": query, "perPage": limit, "minimal_output": False}
            calls.append(Call("search_repositories", base, label=query, baseline=i == 0))
            if i == 0:
                # Popular repositories are worth *considering*; they are not ranked by stars.
                by_stars = {**base, "sort": "stars", "order": "desc"}
                calls.append(Call("search_repositories", by_stars, label=f"{query} (most stars)"))
        return calls

    def parse_search(self, outcome: CallOutcome, limit: int) -> list[Candidate]:
        data = _json(outcome.text)
        items = data.get("items") if isinstance(data, dict) else data
        out = []
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            owner_value = item.get("owner")
            owner: dict[str, Any] = owner_value if isinstance(owner_value, dict) else {}
            full_name = _str(item.get("full_name")) or "/".join(
                p for p in (_str(owner.get("login")), _str(item.get("name"))) if p
            )
            if "/" not in full_name:
                continue
            c = self._new(full_name, _str(item.get("html_url")) or f"https://github.com/{full_name}")
            c.title = _str(item.get("name")) or c.name
            c.description = _str(item.get("description"))
            c.tags = [t for t in item.get("topics") or [] if isinstance(t, str)]
            c.programming_language = _str(item.get("language")) or None
            if "license" in item:
                c.license_reported = True
                lic = item["license"]
                if isinstance(lic, dict):
                    c.license = (
                        _str(lic.get("spdx_id")) or _str(lic.get("key")) or _str(lic.get("name")) or None
                    )
                elif isinstance(lic, str):
                    c.license = lic or None
            # pushed_at is the last code change; updated_at also moves when someone stars the repo.
            c.updated_at = parse_date(item.get("pushed_at")) or parse_date(item.get("updated_at"))
            c.created_at = parse_date(item.get("created_at"))
            _set_metric(c, "stars", item.get("stargazers_count"))
            _set_metric(c, "forks", item.get("forks_count"))
            _set_metric(c, "open_issues", item.get("open_issues_count"))
            for flag in ("archived", "fork", "private", "disabled", "is_template"):
                if item.get(flag) is True:
                    c.flags.add(flag)
            out.append(c)
        return out[:limit]

    def enrich_calls(self, candidates: list[Candidate]) -> list[Call]:
        calls = []
        for c in candidates:
            owner, _, repo = c.id.partition("/")
            calls.append(
                Call(
                    "get_file_contents",
                    {"owner": owner, "repo": repo, "path": "README.md"},
                    "readme",
                    targets=[c.id],
                )
            )
            calls.append(
                Call("get_latest_release", {"owner": owner, "repo": repo}, "release", targets=[c.id])
            )
        return calls

    def apply(self, candidates: dict[str, Candidate], call: Call, outcome: CallOutcome) -> None:
        c = candidates.get(call.targets[0])
        if c is None:
            return
        if call.label == "readme":
            if outcome.ok and outcome.text.strip():
                # The server prefixes the file with a one-line status ("successfully downloaded ...").
                text = re.sub(r"\A\s*successfully downloaded[^\n]*\n", "", outcome.text, flags=re.IGNORECASE)
                c.doc_text = text[:DOC_CHARS]
                c.enriched = True
            else:
                c.read_notes.append("README.md could not be read")
        elif call.label == "release":
            data = _json(outcome.text) if outcome.ok else None
            if isinstance(data, dict) and (data.get("tag_name") or data.get("published_at")):
                c.has_releases = True
                c.released_at = parse_date(data.get("published_at")) or parse_date(data.get("created_at"))
            elif not outcome.ok and re.search(r"404|not found", outcome.error or "", re.IGNORECASE):
                c.has_releases = False
            else:
                c.read_notes.append("latest release could not be read")


# --- Hugging Face ---------------------------------------------------------------------------------

_HF_FIELD = re.compile(r"\*\*([A-Za-z ]+):\*\*\s*([^|\n]*)")
_ISO_639_1 = frozenset(
    "aa ab ae af ak am an ar as av ay az ba be bg bh bi bm bn bo br bs ca ce ch co cr cs cu cv cy da de dv dz ee "
    "el en eo es et eu fa ff fi fj fo fr fy ga gd gl gn gu gv ha he hi ho hr ht hu hy hz ia id ie ig ii ik io is "
    "it iu ja jv ka kg ki kj kk kl km kn ko kr ks ku kv kw ky la lb lg li ln lo lt lu lv mg mh mi mk ml mn mr ms "
    "mt my na nb nd ne ng nl nn no nr nv ny oc oj om or os pa pi pl ps pt qu rm rn ro ru rw sa sc sd se sg si sk "
    "sl sm sn so sq sr ss st su sv sw ta te tg th ti tk tl tn to tr ts tt tw ty ug uk ur uz ve vi vo wa wo xh yi "
    "yo za zh zu".split()
)
_SAFE_FORMATS = frozenset({"safetensors", "gguf", "onnx"})
_PICKLE_FILE = re.compile(
    r"(?:\.(?:pt|pth|pkl|pickle|ckpt|joblib)|(?:pytorch_model|adapter_model|pytorch_lora_weights)[^/]*\.bin)$"
)
_WEIGHT_EXT = {
    ".safetensors": "safetensors",
    ".gguf": "gguf",
    ".onnx": "onnx",
    ".h5": "h5",
    ".tflite": "tflite",
    ".msgpack": "flax",
    ".ot": "rust",
    ".mlmodel": "coreml",
}


def apply_hf_tags(c: Candidate, tags: list[str]) -> None:
    """Read what Hub tags say: license, languages, lineage, weight formats, eval results."""
    known = set(c.tags)
    c.tags.extend(t for t in tags if t not in known)
    formats: set[str] = set()
    for tag in tags:
        low = tag.lower()
        if low.startswith("license:"):
            c.license, c.license_reported = tag.split(":", 1)[1], True
        elif low.startswith("language:"):
            _add_language(c, low.split(":", 1)[1])
        elif low in _ISO_639_1 and c.need == "model":
            _add_language(c, low)
        elif low == "multilingual" or low.startswith("multilinguality:multilingual"):
            _add_language(c, "multilingual")
        elif low.startswith("base_model:"):
            parts = tag.split(":")
            if len(parts) == 2:
                c.base_model = c.base_model or parts[1]
            elif len(parts) >= 3:
                c.lineage, c.base_model = parts[1].lower(), ":".join(parts[2:])
        elif low in ("safetensors", "gguf", "onnx"):
            formats.add(low)
        elif low in ("pytorch", "tf", "jax", "rust", "coreml", "tflite", "openvino"):
            formats.add("pickle" if low == "pytorch" else low)
        elif low in ("custom_code", "model-index"):
            c.flags.add(low)
        elif low.startswith("source_datasets:original"):
            c.flags.add("original_source")
    if formats and c.weight_formats is None:
        # From tags only: a hint for the shortlist, confirmed later by listing the files.
        c.weight_formats = formats
        c.flags.add("weights_from_tags")


def _add_language(c: Candidate, code: str) -> None:
    if code and code not in c.languages:
        c.languages.append(code)


def split_front_matter(text: str) -> tuple[str, str, bool]:
    """(YAML front matter, body, complete) of a README.

    ``complete`` is False when the text was cut off inside the front matter (the fetch has a
    byte limit), so the body was never reached.
    """
    m = re.match(r"\A\s*---\s*\n(.*?)\n---\s*(?:\n|\Z)", text, flags=re.DOTALL)
    if m:
        return m.group(1), text[m.end() :], True
    if re.match(r"\A\s*---\s*\n", text):
        return text, "", False
    return "", text, True


def read_front_matter(yaml_text: str) -> dict[str, list[str]]:
    """Top-level scalar and simple list values of a card's YAML front matter (no YAML library)."""
    out: dict[str, list[str]] = {}
    key: str | None = None
    for line in yaml_text.splitlines():
        top = re.match(r"^([A-Za-z_][\w-]*):\s*(.*)$", line)
        if top:
            key, value = top.group(1), top.group(2).strip()
            out.setdefault(key, [])
            if value.startswith("[") and value.endswith("]"):
                out[key].extend(v.strip().strip("'\"") for v in value[1:-1].split(",") if v.strip())
            elif value and value not in (">-", "|", ">", "|-"):
                out[key].append(value.strip("'\""))
            continue
        # A plain list item directly under a top-level key ("- sst2"); nested mappings have a colon.
        item = re.match(r"^\s{0,2}-\s+([^:\n]+?)\s*$", line)
        if key and item:
            out[key].append(item.group(1).strip("'\""))
    return out


def read_model_index(yaml_text: str, limit: int = 24) -> list[EvalResult]:
    """Eval results from a card's ``model-index`` block.

    Each result records what it was measured on (dataset id, config, split) as well as the metric,
    value and whether Hugging Face verified it: numbers are only comparable on the same benchmark.
    """
    start = re.search(r"^model-index:", yaml_text, flags=re.MULTILINE)
    if not start:
        return []
    results: list[EvalResult] = []
    entry: list[EvalResult] = []  # the metrics of the result entry being read
    dataset: dict[str, str] = {}
    mode, metric, in_sub_block = "", None, False

    def close_entry() -> None:
        for result in entry:
            # The Hub id ("glue") identifies a benchmark across cards better than its display name.
            result.dataset = dataset.get("type") or dataset.get("name") or "unnamed dataset"
            result.config, result.split = dataset.get("config", ""), dataset.get("split", "")
        entry.clear()
        dataset.clear()

    for line in yaml_text[start.end() :].splitlines():
        if re.match(r"^[A-Za-z_]", line):  # next top-level key
            break
        s = line.strip()
        starts_item = s.startswith("- ")
        bare = s[2:].strip() if starts_item else s
        if bare in ("dataset:", "task:"):
            if starts_item:
                close_entry()  # "- task:" opens the next result
            mode = bare[:-1]
        elif bare.startswith("metrics:"):
            mode = "metrics"
        elif mode == "dataset":
            name, _, value = s.partition(":")
            if name in ("name", "type", "config", "split") and value.strip():
                dataset[name] = value.strip().strip("'\"")
        elif mode == "metrics":
            name, _, value = bare.partition(":")
            name, value = name.strip(), value.strip().strip("'\"")
            if starts_item:
                metric, in_sub_block = EvalResult(dataset="", metric="", value=None), False
                entry.append(metric)
                results.append(metric)
            elif name in ("source", "args"):
                in_sub_block = True  # their own name/url fields are not the metric's
            if metric is None or in_sub_block:
                continue
            if name == "type" and value:
                metric.metric = value  # the metric id ("accuracy") is preferred over its display name
            elif name == "name" and not metric.metric:
                metric.metric = value
            elif name == "value":
                metric.value = parse_number(value)
            elif name == "verified":
                metric.verified = value.lower() == "true"
    close_entry()
    return [r for r in results if r.metric][:limit]


SAMPLE_ROWS = 30  # rows of a dataset looked at for empty cells and duplicates
PREVIEW_TOP = 4  # datasets per read whose sample rows are fetched
_EMPTY_CELLS = frozenset({"", "null", "none", "nan", "n/a", "na"})


_VIEWER_BUSY = re.compile(
    r"Could not fetch [^\n]*?(?:\b429\b|\b502\b|\b503\b|\b504\b|timeout|timed out|aborted)", re.IGNORECASE
)


def _viewer_is_busy(text: str) -> bool:
    """The Hub's dataset viewer answered with a rate limit, gateway error or timeout, not with a verdict."""
    return _VIEWER_BUSY.search(text) is not None


def _table_rows(section: str) -> list[list[str]]:
    """Cells of each data row of a Markdown table (header and separator lines dropped)."""
    rows = []
    for line in section.splitlines():
        line = line.strip()
        if not line.startswith("|") or re.fullmatch(r"\|[\s:|-]+\|?", line):
            continue
        cells = [
            cell.replace("\\_", "_").replace("\\|", "|").strip()
            for cell in re.split(r"(?<!\\)\|", line)[1:-1]
        ]
        rows.append(cells)
    return rows[1:]  # without the header row


def _section(body: str, heading: str) -> str:
    found = re.search(rf"^###\s+{re.escape(heading)}\n(.*?)(?=^##|\Z)", body, flags=re.MULTILINE | re.DOTALL)
    return found.group(1) if found else ""


def read_dataset_structure(details: str) -> DatasetStructure | None:
    """Splits, row counts, columns and label classes from Hugging Face's ``dataset_structure`` text.

    None when the dataset has no viewer (the Hub could not load its files), which is itself a fact
    worth knowing: such a dataset cannot be previewed or loaded without custom code.
    """
    structure = DatasetStructure()
    for cells in _table_rows(_section(details, "Configs and Splits")):
        if len(cells) < 4:
            continue
        rows = parse_number(cells[2])
        if rows is None:
            rows = parse_number(cells[3])  # an estimate, when the exact count is not computed
        structure.splits[f"{cells[0]}/{cells[1]}"] = rows
    total = re.search(r"^Total rows:\s*([0-9.,]+[KMB]?)", details, flags=re.MULTILINE)
    structure.total_rows = parse_number(total.group(1)) if total else None
    if not structure.total_rows and any(rows is None for rows in structure.splits.values()):
        structure.total_rows = None  # "Total rows: 0" next to uncounted splits means unknown, not empty
    for cells in _table_rows(_section(details, "Schema Preview")):
        if len(cells) < 3:
            continue
        structure.columns.append(cells[1])
        feature = _json(cells[2].replace("\\", ""))
        if isinstance(feature, dict) and feature.get("_type") == "ClassLabel":
            structure.label_names = [str(n) for n in feature.get("names") or []][:50]
    structure.column_count = len(structure.columns) or None
    if not structure.splits and not structure.columns:
        return None
    if structure.splits:
        first_config = next(iter(structure.splits)).rsplit("/", 1)[0]
        in_config = [k.rsplit("/", 1)[1] for k in structure.splits if k.rsplit("/", 1)[0] == first_config]
        structure.preview_of = (first_config, "train" if "train" in in_config else in_config[0])
    return structure


def read_sample_rows(structure: DatasetStructure, preview: str) -> None:
    """Count empty cells and repeated rows in the first rows of a dataset.

    This looks at the head of one split only. It can show that a problem exists; it cannot show
    that none does, and it says nothing about class balance (many datasets are sorted by label).
    """
    section = _section(preview, "Rows")
    header = next((line for line in section.splitlines() if line.strip().startswith("|")), "")
    width = len(re.split(r"(?<!\\)\|", header.strip())) - 2
    rows = [cells[1:] for cells in _table_rows(section) if len(cells) == width and cells[0].isdigit()]
    if not rows:
        return
    structure.sample_rows = len(rows)
    structure.sample_empty_cells = sum(cell.lower() in _EMPTY_CELLS for row in rows for cell in row)
    structure.sample_duplicate_rows = len(rows) - len({tuple(row) for row in rows})


# Task names the Hub files models (pipeline tags) and datasets (task categories) under.
HUB_TASKS = frozenset(
    "text-classification token-classification question-answering table-question-answering summarization "
    "translation text-generation fill-mask sentence-similarity feature-extraction text-ranking "
    "zero-shot-classification automatic-speech-recognition text-to-speech text-to-audio audio-classification "
    "voice-activity-detection image-classification object-detection image-segmentation image-to-text "
    "image-text-to-text text-to-image image-to-image image-feature-extraction zero-shot-image-classification "
    "zero-shot-object-detection visual-question-answering document-question-answering depth-estimation "
    "video-classification text-to-video tabular-classification tabular-regression time-series-forecasting "
    "reinforcement-learning mask-generation keypoint-detection".split()
)


def hub_task(query: str) -> str | None:
    """The Hub task a search phrasing names, if it names one: 'Sentence similarity' -> 'sentence-similarity'."""
    task = re.sub(r"[\s_]+", "-", query.strip().lower())
    return task if task in HUB_TASKS else None


class HuggingFace(Adapter):
    source = "huggingface"

    def __init__(self, need: Need) -> None:
        self.need = need
        self.kind = "model" if need == "model" else "dataset"

    def _url(self, repo_id: str) -> str:
        return f"https://huggingface.co/{'datasets/' if self.kind == 'dataset' else ''}{repo_id}"

    def search_calls(self, queries: list[str], limit: int, languages: list[str] | None = None) -> list[Call]:
        calls = []
        for i, query in enumerate(queries):
            task = hub_task(query)
            if task is not None:
                # A phrasing that names a Hub task ("sentence similarity") browses that task instead
                # of searching names: the standard models for a task rarely have the task in their name
                # (all-MiniLM-L6-v2, bge-m3). The most downloaded are asked for, as candidates to
                # consider, not as a ranking. Trending was tried too and dropped: on the seed
                # evaluation it filled the list with that week's uploads (docs/evaluation.md).
                filters = [task if self.kind == "model" else f"task_categories:{task}"]
                if languages:
                    filters.append(languages[0] if self.kind == "model" else f"language:{languages[0]}")
                browse = {"repo_types": [self.kind], "filters": filters, "limit": limit, "sort": "downloads"}
                calls.append(
                    Call("hub_repo_search", browse, label=f"task {task} (most downloads)", baseline=i == 0)
                )
                continue
            base = {"query": query, "repo_types": [self.kind], "limit": limit}
            calls.append(Call("hub_repo_search", base, label=query, baseline=i == 0))
            if i == 0:
                calls.append(
                    Call("hub_repo_search", {**base, "sort": "downloads"}, label=f"{query} (most downloads)")
                )
        return calls

    def parse_search(self, outcome: CallOutcome, limit: int) -> list[Candidate]:
        out = []
        for block in re.split(r"^###\s+", outcome.text.replace("\r", ""), flags=re.MULTILINE)[1:]:
            head, _, body = block.partition("\n")
            repo_id = head.strip()
            if "/" not in repo_id or " " in repo_id:
                continue
            c = self._new(repo_id, self._url(repo_id))
            c.title = c.name
            fields = {k.strip().lower(): v.strip() for k, v in _HF_FIELD.findall(body)}
            first_field = body.find("**")
            blurb = body[: first_field if first_field >= 0 else len(body)]
            c.description = re.sub(r"\s+", " ", blurb).strip()[:600]
            c.task = fields.get("task") or None
            c.library = fields.get("library") or None
            _set_metric(c, "downloads", fields.get("downloads"))
            _set_metric(c, "likes", fields.get("likes"))
            c.created_at = parse_date(fields.get("created"))
            c.updated_at = parse_date(fields.get("last modified"))
            apply_hf_tags(c, [t.strip() for t in fields.get("tags", "").split(",") if t.strip()])
            if re.search(r"\*and \d+ more", body):
                c.flags.add("tags_truncated")
            out.append(c)
        return out[:limit]

    def enrich_calls(self, candidates: list[Candidate]) -> list[Call]:
        calls = []
        ids = [c.id for c in candidates]
        for i in range(0, len(ids), 10):  # hub_repo_details takes at most 10 repos
            chunk = ids[i : i + 10]
            arguments: dict[str, Any] = {"repo_ids": chunk, "repo_type": self.kind}
            if self.kind == "dataset":
                # Splits, row counts and the column schema, from the dataset viewer.
                arguments["operations"] = ["overview", "dataset_structure"]
            calls.append(Call("hub_repo_details", arguments, "details", targets=chunk))
        per_call = 15 if self.kind == "model" else 30  # hf_fs takes at most 30 operations
        root = "hf://models/" if self.kind == "model" else "hf://datasets/"
        for i in range(0, len(ids), per_call):
            chunk = ids[i : i + per_call]
            operations, targets = [], []
            for repo_id in chunk:
                operations.append(
                    {"cmd": "cat", "args": [f"{root}{repo_id}/README.md", "--max-bytes", "6000"]}
                )
                targets.append(repo_id)
                if self.kind == "model":
                    operations.append({"cmd": "ls", "args": [f"{root}{repo_id}"]})
                    targets.append(repo_id)
            calls.append(Call("hf_fs", {"operations": operations}, "files", targets=targets))
        return calls

    def follow_up_calls(self, candidates: list[Candidate]) -> list[Call]:
        """Sample rows of each dataset whose structure is known (the preview needs a config and split)."""
        calls = []
        # Only the leading few: every preview is a request to the Hub's dataset viewer, which
        # rate-limits anonymous callers.
        for c in candidates[:PREVIEW_TOP]:
            if self.kind != "dataset" or c.structure is None or c.structure.preview_of is None:
                continue
            if c.flags & {"viewer_failed", "viewer_unavailable"}:
                continue  # the Hub already said it cannot load these rows, or is not answering
            config, split = c.structure.preview_of
            arguments = {
                "repo_ids": [c.id],
                "repo_type": "dataset",
                "operations": ["dataset_preview"],
                "config": config,
                "split": split,
                "limit": SAMPLE_ROWS,
            }
            calls.append(Call("hub_repo_details", arguments, "preview", targets=[c.id]))
        return calls

    def apply(self, candidates: dict[str, Candidate], call: Call, outcome: CallOutcome) -> None:
        if call.label == "details":
            if outcome.ok:
                self._apply_details(candidates, outcome.text)
            return
        if call.label == "preview":
            c = candidates.get(call.targets[0])
            if c is not None and c.structure is not None and outcome.ok:
                read_sample_rows(c.structure, outcome.text)
                if c.structure.sample_rows is None:
                    c.flags.add("viewer_unavailable" if _viewer_is_busy(outcome.text) else "viewer_failed")
            return
        results = outcome.structured.get("results") if isinstance(outcome.structured, dict) else None
        if not outcome.ok or not isinstance(results, list):
            for repo_id in dict.fromkeys(call.targets):
                if repo_id in candidates:
                    candidates[repo_id].read_notes.append("card and file list could not be read")
            return
        for entry in results:
            index = entry.get("index") if isinstance(entry, dict) else None
            if not isinstance(index, int) or not 0 <= index < len(call.targets):
                continue
            c = candidates.get(call.targets[index])
            if c is None:
                continue
            op = call.arguments["operations"][index]["cmd"]
            result = entry.get("result") if entry.get("status") == "success" else None
            if not isinstance(result, dict):
                error = entry.get("error") if isinstance(entry.get("error"), dict) else {}
                if (
                    "ACCESS_DENIED" in _str(error.get("code"))
                    or "gated" in _str(error.get("message")).lower()
                ):
                    c.flags.add("access_restricted")
                c.read_notes.append(
                    "card could not be read" if op == "cat" else "file list could not be read"
                )
            elif op == "cat":
                self._apply_card(c, _str(result.get("content")))
            else:
                self._apply_files(c, result.get("entries"))

    def _apply_details(self, candidates: dict[str, Candidate], text: str) -> None:
        for block in re.split(r"^#\s+(?=\S+/\S+\s*$)", text.replace("\r", ""), flags=re.MULTILINE)[1:]:
            head, _, body = block.partition("\n")
            c = candidates.get(head.strip())
            if c is None:
                continue
            fields = {k.strip().lower(): v.strip() for k, v in _HF_FIELD.findall(body)}
            c.updated_at = parse_date(fields.get("updated")) or c.updated_at
            c.task = c.task or fields.get("task") or None
            c.library = c.library or fields.get("library") or None
            params = parse_number(fields.get("parameters"))
            if params is not None:
                c.params = params
            # The details page shows all-time downloads; search showed the last 30 days.
            _set_metric(c, "downloads_all_time", fields.get("downloads"))
            tags_section = re.search(r"^## Tags\n(.*?)(?=^## |\Z)", body, flags=re.MULTILINE | re.DOTALL)
            if tags_section:
                apply_hf_tags(c, re.findall(r"`([^`]+)`", tags_section.group(1)))
                c.flags.discard("tags_truncated")
            spaces = re.search(r"^## Demo Spaces\n(.*?)(?=^## |\Z)", body, flags=re.MULTILINE | re.DOTALL)
            if spaces:
                more = re.search(r"and (\d+) more", spaces.group(1))
                listed = len(re.findall(r"^- \[", spaces.group(1), flags=re.MULTILINE))
                c.metrics["spaces"] = float(listed + (int(more.group(1)) if more else 0))
            if re.search(r"^## Inference Providers", body, flags=re.MULTILINE):
                c.flags.add("inference_providers")
            description = re.search(
                r"^## Description\n(.*?)(?=^## |\Z)", body, flags=re.MULTILINE | re.DOTALL
            )
            if description:
                summary = re.sub(r"\s+", " ", description.group(1)).strip()
                c.description = c.description or summary[:600]
                c.doc_text = c.doc_text or summary[:DOC_CHARS]  # until the card itself is read
            if self.kind == "dataset":
                c.structure = read_dataset_structure(body) or c.structure
                if c.structure is None and "arbitrary Python code" in body:
                    c.flags.add("loading_script")  # the Hub refuses to run it, so there is no viewer
                elif _viewer_is_busy(body):
                    c.flags.add("viewer_unavailable")  # a rate limit or outage: says nothing about the data
                elif c.structure is None and "Could not fetch splits" in body:
                    c.flags.add("no_viewer")
                elif "Failed parquet jobs" in body or "Could not fetch rows" in body:
                    c.flags.add("viewer_failed")  # the Hub tried to load the files and could not

    def _apply_card(self, c: Candidate, readme: str) -> None:
        front, body, complete = split_front_matter(readme)
        meta = read_front_matter(front)
        if meta.get("license"):
            c.license = meta["license"][0]
        c.license_reported = True  # the card was read: a missing license means none is declared
        for code in meta.get("language", []):
            _add_language(c, code.lower())
        if meta.get("base_model") and not c.base_model:
            c.base_model = meta["base_model"][0]
        c.library = c.library or next(iter(meta.get("library_name", [])), None)
        c.task = c.task or next(iter(meta.get("pipeline_tag", [])), None)
        known = set(c.tags)
        c.tags.extend(t for t in meta.get("tags", []) if t not in known)
        c.evals = read_model_index(front)
        if c.evals:
            c.flags.add("model-index")
        if body.strip():
            c.doc_text = body.strip()[:DOC_CHARS]
        elif not complete:
            # A long metadata block: the text of the card lies beyond what was fetched.
            c.flags.add("card_cut_off")
            c.read_notes.append("only the start of the card was read (it has a long metadata block)")
        c.enriched = True

    def _apply_files(self, c: Candidate, entries: Any) -> None:
        if not isinstance(entries, list):
            return
        formats: set[str] = set()
        sizes: dict[str, int] = {}
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            path = _str(entry.get("path")).lower()
            if entry.get("type") == "dir":
                if path in ("onnx", "openvino"):
                    formats.add(path)
                continue
            size_value = entry.get("size")
            size = size_value if isinstance(size_value, int) else 0
            fmt = (
                "pickle"
                if _PICKLE_FILE.search(path)
                else next((name for ext, name in _WEIGHT_EXT.items() if path.endswith(ext)), None)
            )
            if fmt:
                formats.add(fmt)
                sizes[fmt] = max(sizes.get(fmt, 0), size)
        c.weight_formats = formats
        c.flags.discard("weights_from_tags")
        if sizes:
            # The size of one copy of the weights, preferring the format people should load.
            preferred = next((f for f in ("safetensors", "gguf", "onnx", "pickle") if f in sizes), None)
            c.size_bytes = sizes[preferred] if preferred else max(sizes.values())


def pickle_only(c: Candidate) -> bool | None:
    """True when the only weights are pickle files; None when the formats are not known."""
    if c.weight_formats is None or not c.weight_formats:
        return None
    return "pickle" in c.weight_formats and len(c.weight_formats) == 1


def has_safe_weights(c: Candidate) -> bool | None:
    if c.weight_formats is None or not c.weight_formats:
        return None
    return bool(c.weight_formats & _SAFE_FORMATS)


# --- Kaggle ---------------------------------------------------------------------------------------


def _kaggle_tags(item: dict[str, Any]) -> list[str]:
    return [
        _str(t.get("name")) for t in item.get("tags") or [] if isinstance(t, dict) and _str(t.get("name"))
    ]


def _kaggle_items(text: str, *keys: str) -> list[dict[str, Any]]:
    data = _json(text)
    if isinstance(data, dict):
        for key in keys:
            if isinstance(data.get(key), list):
                data = data[key]
                break
    return [i for i in data if isinstance(i, dict)] if isinstance(data, list) else []


class KaggleDatasets(Adapter):
    source = "kaggle"
    need: Need = "dataset"

    def search_calls(self, queries: list[str], limit: int, languages: list[str] | None = None) -> list[Call]:
        calls = []
        for i, query in enumerate(queries):
            base = {"search": query, "pageSize": limit}
            calls.append(
                Call(
                    "search_datasets",
                    {"request": {**base, "sortBy": "Relevance"}},
                    label=query,
                    baseline=i == 0,
                )
            )
            if i == 0:
                # How many notebooks were built on a dataset is Kaggle's best sign of real use.
                calls.append(
                    Call(
                        "search_datasets",
                        {"request": {**base, "sortBy": "NotebookCount"}},
                        label=f"{query} (most notebooks)",
                    )
                )
        return calls

    def parse_search(self, outcome: CallOutcome, limit: int) -> list[Candidate]:
        out = []
        for item in _kaggle_items(outcome.text, "datasets"):
            ref = _str(item.get("ref"))
            if "/" not in ref:
                continue
            c = self._new(ref, _str(item.get("url")) or f"https://www.kaggle.com/datasets/{ref}")
            c.title = _str(item.get("title")) or c.name
            c.description = _str(item.get("subtitle"))
            c.tags = _kaggle_tags(item)
            c.license = _str(item.get("license_name")) or None
            c.license_reported = "license_name" in item
            c.updated_at = parse_date(item.get("last_updated"))
            for metric, key in (
                ("downloads", "download_count"),
                ("notebooks", "kernel_count"),
                ("votes", "vote_count"),
                ("views", "view_count"),
                ("usability", "usability_rating"),
                ("versions", "current_version_number"),
            ):
                _set_metric(c, metric, item.get(key))
            size = parse_number(item.get("total_bytes"))
            c.size_bytes = int(size) if size is not None else None
            out.append(c)
        return out[:limit]  # Kaggle ignores pageSize here and returns a full page

    def enrich_calls(self, candidates: list[Candidate]) -> list[Call]:
        calls = []
        for c in candidates:
            owner, _, slug = c.id.partition("/")
            request = {"request": {"ownerSlug": owner, "datasetSlug": slug}}
            calls.append(Call("get_dataset_info", request, "info", targets=[c.id]))
            # File types and column counts. Kaggle's server offers no row counts or sample rows.
            calls.append(Call("get_dataset_files_summary", request, "files", targets=[c.id]))
        return calls

    def apply(self, candidates: dict[str, Candidate], call: Call, outcome: CallOutcome) -> None:
        c = candidates.get(call.targets[0])
        if c is None:
            return
        data = _json(outcome.text) if outcome.ok else None
        if call.label == "files":
            if isinstance(data, dict):
                files = _dict(data.get("file_summary_info"))
                columns = _dict(data.get("column_summary_info"))
                types = [
                    _str(t.get("extension")).lower()
                    for t in files.get("file_types") or []
                    if isinstance(t, dict) and _str(t.get("extension"))
                ]
                count = parse_number(columns.get("total_column_count"))
                if types or count:
                    c.structure = DatasetStructure(
                        file_types=types, column_count=int(count) if count else None
                    )
            return
        if not isinstance(data, dict):
            c.read_notes.append("dataset description could not be read")
            return
        c.doc_text = _str(data.get("description"))[:DOC_CHARS]
        if isinstance(data.get("versions"), list):
            c.metrics["versions"] = float(len(data["versions"]))
        c.enriched = True


class KaggleModels(Adapter):
    source = "kaggle"
    need: Need = "model"
    reads_shortlist = False  # list_models already returns each model's card

    def search_calls(self, queries: list[str], limit: int, languages: list[str] | None = None) -> list[Call]:
        calls = []
        for i, query in enumerate(queries):
            base = {"search": query, "pageSize": limit}
            calls.append(Call("list_models", {"request": base}, label=query, baseline=i == 0))
            if i == 0:
                calls.append(
                    Call(
                        "list_models",
                        {"request": {**base, "sortBy": "DownloadCount"}},
                        label=f"{query} (most downloads)",
                    )
                )
        return calls

    def parse_search(self, outcome: CallOutcome, limit: int) -> list[Candidate]:
        out = []
        for item in _kaggle_items(outcome.text, "models"):
            ref = _str(item.get("ref"))
            if "/" not in ref:
                continue
            c = self._new(ref, _str(item.get("url")) or f"https://www.kaggle.com/models/{ref}")
            c.title = _str(item.get("title")) or c.name
            c.description = _str(item.get("subtitle"))
            c.tags = _kaggle_tags(item)
            c.updated_at = parse_date(item.get("update_time"))
            c.created_at = parse_date(item.get("publish_time"))
            _set_metric(c, "votes", item.get("vote_count"))
            instances = [i for i in item.get("instances") or [] if isinstance(i, dict)]
            licenses = [_str(i.get("license_name")) for i in instances if _str(i.get("license_name"))]
            if licenses:
                c.license, c.license_reported = max(set(licenses), key=licenses.count), True
            frameworks = sorted(
                {
                    _str(i.get("framework")).replace("MODEL_FRAMEWORK_", "").replace("_", "").lower()
                    for i in instances
                    if _str(i.get("framework"))
                }
                - {"other", "unspecified", ""}
            )
            c.library = ", ".join(frameworks) or None
            c.tags.extend(frameworks)
            sizes = [s for s in (parse_number(i.get("total_uncompressed_bytes")) for i in instances) if s]
            c.size_bytes = int(min(sizes)) if sizes else None
            overviews = " ".join(_str(i.get("overview")) for i in instances[:3])
            c.doc_text = (_str(item.get("description")) + "\n" + overviews).strip()[:DOC_CHARS]
            if _str(item.get("provenance_sources")):
                c.flags.add("provenance_stated")
            c.enriched = True
            out.append(c)
        return out[:limit]


class KaggleNotebooks(Adapter):
    """Needs a Kaggle token. Field names follow Kaggle's API and are NOT verified live yet."""

    source = "kaggle"
    need: Need = "notebook"
    reads_shortlist = False

    def search_calls(self, queries: list[str], limit: int, languages: list[str] | None = None) -> list[Call]:
        calls = []
        for i, query in enumerate(queries):
            base = {"search": query, "pageSize": limit}
            calls.append(
                Call(
                    "search_notebooks",
                    {"request": {**base, "sortBy": "Relevance"}},
                    label=query,
                    baseline=i == 0,
                )
            )
            if i == 0:
                calls.append(
                    Call(
                        "search_notebooks",
                        {"request": {**base, "sortBy": "VoteCount"}},
                        label=f"{query} (most votes)",
                    )
                )
        return calls

    def parse_search(self, outcome: CallOutcome, limit: int) -> list[Candidate]:
        out = []
        for item in _kaggle_items(outcome.text, "kernels", "notebooks"):
            ref = _str(item.get("ref"))
            if "/" not in ref:
                continue
            c = self._new(ref, _str(item.get("url")) or f"https://www.kaggle.com/code/{ref}")
            c.title = _str(item.get("title")) or c.name
            c.tags = _kaggle_tags(item)
            c.programming_language = _str(item.get("language")) or None
            c.updated_at = parse_date(item.get("last_run_time")) or parse_date(item.get("last_updated"))
            _set_metric(c, "votes", item.get("total_votes", item.get("vote_count")))
            c.enriched = True
            out.append(c)
        return out[:limit]


ADAPTERS: dict[tuple[str, Need], Adapter] = {
    ("github", "code"): GitHubCode(),
    ("huggingface", "model"): HuggingFace("model"),
    ("huggingface", "dataset"): HuggingFace("dataset"),
    ("kaggle", "dataset"): KaggleDatasets(),
    ("kaggle", "model"): KaggleModels(),
    ("kaggle", "notebook"): KaggleNotebooks(),
}
