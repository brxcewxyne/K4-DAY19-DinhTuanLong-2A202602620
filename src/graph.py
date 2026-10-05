"""Knowledge Graph (Neo4j) + GraphRAG over two drug-topic knowledge bases.

Contract (fixed — bench_kg.py and the tests rely on it):
    link_entity(name, known)                       -> one of `known` or None          (TODO KG-1)
    build_graph(graph, law_docs, news_docs, llm_fn)   load both KBs into Neo4j      (TODO KG-2)
        every node created from ONE document carries the property `doc_id`
    Neo4jGraph.context(question, doc_ids)         -> list[str] facts               (TODO KG-3)
    GraphRAGAgent.answer(question, top_k)         -> str                           (TODO KG-4)

Everything else in this file is a HINT: one possible ontology (below). Use it as is, change it,
or design your own — your own ontology + report/ONTOLOGY.md earns the bonus (see SUBMISSION.md).

Suggested ontology (Crime is the bridge between the law KB and the news KB):

    (:Article {id, title, law, doc_id})-[:DEFINES]->(:Crime {name})
    (:Article)-[:HAS_CLAUSE]->(:Clause {id, number, penalty, text})-[:MENTIONS]->(:Substance {name})
    (:Case {name, summary, date, doc_id})-[:CHARGED_WITH]->(:Crime)
    (:Case)-[:INVOLVES {amount}]->(:Substance)
    (:Case)-[:LOCATED_IN]->(:Location {name})
    (:Person {name, aliases})-[:INVOLVED_IN {role, sentence, charge}]->(:Case)
"""

from __future__ import annotations

import difflib
import inspect
import json
import re
import unicodedata
from pathlib import Path
from typing import Any, Callable

from .models import Document
from .store import EmbeddingStore

# Canonical substance names: the ones BLHS Chương XX lists, plus common ones in Vietnamese news.
SUBSTANCES = ["Heroine", "Cocaine", "Methamphetamine", "Amphetamine", "MDMA", "XLR-11", "Ketamine",
              "cần sa", "thuốc phiện", "côca"]
CLAUSE_START = re.compile(r"^(\d+)\.\s", re.MULTILINE)
FOOTNOTE = re.compile(r"\[\d+\]")
POINT_START = re.compile(r"^([a-zđ])\)\s", re.MULTILINE)
PENALTY_PHRASE = re.compile(r"\bbị\s+((?:phạt|tù|cảnh cáo).+?)(?::|$)")
PRISON_RANGE = re.compile(r"từ\s+([\d.,]+)\s*(năm|tháng)\s+đến\s+([\d.,]+)\s*(năm|tháng)")
PRISON_SINGLE = re.compile(r"\btù\s+([\d.,]+)\s*(năm|tháng)")
QUANTITY = re.compile(
    r"(?:có\s+)?(?P<marker>khối lượng|thể tích|số lượng)\s+"
    r"(?:(?:từ\s+(?P<min>[\d.,]+)\s*(?P<min_unit>gam|kilôgam|kg|mililít|ml|cây)\s+đến\s+"
    r"(?P<below>dưới\s+)?(?P<max>[\d.,]+)\s*(?P<max_unit>gam|kilôgam|kg|mililít|ml|cây))"
    r"|(?:(?P<single>[\d.,]+)\s*(?P<single_unit>gam|kilôgam|kg|mililít|ml|cây)\s+trở\s+lên))"
)
EQUIVALENCE = re.compile(r"Có 02 chất ma túy trở lên|tương đương|quy đổi")
UNIT_MAP = {"gam": ("g", 1), "kilôgam": ("g", 1000), "kg": ("g", 1000),
            "mililít": ("ml", 1), "ml": ("ml", 1), "cây": ("plant", 1)}

def load_markdown_docs(folder: str | Path) -> list[Document]:
    """Read crawler output (.md with a flat `key: "value"` front matter) into Documents."""
    docs = []
    for path in sorted(Path(folder).glob("*.md")):
        raw = path.read_text(encoding="utf-8")
        _, front, body = raw.split("---", 2)
        metadata = {k: json.loads(v) for k, v in re.findall(r'^(\w+): (".*")$', front, re.MULTILINE)}
        docs.append(Document(id=metadata.get("doc_id", path.stem), content=body.strip(), metadata=metadata))
    return docs

def normalize_crime(name: str) -> str:
    """'Tội Mua bán trái phép chất ma túy' -> 'mua bán trái phép chất ma túy'."""
    name = re.sub(r"\s+", " ", name.strip().strip("\"'“”").lower())
    return name.removeprefix("tội ").strip()

def link_entity(name: str, known: list[str], normalize: Callable[[str], str] = normalize_crime) -> str | None:
    """Map a free-text mention (e.g. a charge written by a journalist) onto one canonical name in `known`."""
    if not name or not known:
        return None
    target = normalize(name)
    if not target:
        return None
    normalized = [normalize(candidate) for candidate in known]
    for index, candidate in enumerate(normalized):
        if candidate and candidate == target:
            return known[index]
    matches = difflib.get_close_matches(target, normalized, n=1, cutoff=0.8)
    if not matches:
        return None
    return known[normalized.index(matches[0])]

def find_substances(text: str) -> list[str]:
    lowered = text.lower()
    return [name for name in SUBSTANCES if name.lower() in lowered]

# ----------------------------------------------------------------------------------------------
# HINT — suggested ontology: extraction helpers
# ----------------------------------------------------------------------------------------------

def _to_number(token: str) -> float | int | None:
    token = token.strip().replace(".", "").replace(",", ".")
    try:
        value = float(token)
    except ValueError:
        return None
    return int(value) if value == int(value) else value

def _clean_number(value: float | int | None) -> float | int | None:
    if value is None:
        return None
    if abs(value - round(value)) < 1e-9:
        return int(round(value))
    return round(value, 9)

def _to_months(token: str, unit: str) -> int | None:
    value = _to_number(token)
    if value is None:
        return None
    return int(round(value * 12)) if unit == "năm" else int(round(value))

def _parse_penalty(clause_text: str) -> dict[str, Any]:
    lead = clause_text.split("\n", 1)[0]
    phrase = PENALTY_PHRASE.search(lead)
    penalty_raw = phrase.group(1).rstrip(".") if phrase else ""
    min_months = max_months = None
    months_range = PRISON_RANGE.search(lead)
    if months_range:
        min_months = _to_months(months_range.group(1), months_range.group(2))
        max_months = _to_months(months_range.group(3), months_range.group(4))
    else:
        months_single = PRISON_SINGLE.search(lead)
        if months_single:
            min_months = _to_months(months_single.group(1), months_single.group(2))
    penalty_life = "chung thân" in lead
    penalty_death = "tử hình" in lead
    has_prison = min_months is not None or max_months is not None or penalty_life or penalty_death
    has_fine = "phạt tiền" in lead
    if has_prison and has_fine:
        penalty_kind = "mixed"
    elif has_prison:
        penalty_kind = "prison"
    elif has_fine:
        penalty_kind = "fine"
    elif penalty_raw:
        penalty_kind = "other"
    else:
        penalty_kind = "none"
    return {
        "penalty_raw": penalty_raw,
        "penalty_min_months": min_months,
        "penalty_max_months": max_months,
        "penalty_life": penalty_life,
        "penalty_death": penalty_death,
        "penalty_kind": penalty_kind,
    }

def _parse_thresholds(point_id: str, point_text: str, doc_id: str) -> list[dict[str, Any]]:
    text = re.sub(r"\s+", " ", point_text).strip()
    if EQUIVALENCE.search(text):
        return [{
            "id": f"{point_id}|equivalence|none|none|none",
            "kind": "equivalence",
            "min_value": None,
            "max_value": None,
            "unit": None,
            "min_inclusive": False,
            "max_inclusive": False,
            "raw_text": text.rstrip("."),
            "substance_group_raw": "",
            "doc_id": doc_id,
        }]
    thresholds = []
    for match in QUANTITY.finditer(text):
        unit_raw = match.group("min_unit") or match.group("single_unit")
        unit, factor = UNIT_MAP[unit_raw]
        if match.group("single") is not None:
            min_value = _to_number(match.group("single")) * factor
            max_value = None
            min_inclusive, max_inclusive = True, False
            kind = "quantity_min"
        else:
            min_value = _to_number(match.group("min")) * factor
            max_value = _to_number(match.group("max")) * UNIT_MAP[match.group("max_unit")][1]
            min_inclusive, max_inclusive = True, match.group("below") is None
            kind = "quantity_range"
        thresholds.append({
            "id": f"{point_id}|{kind}|{unit}|{min_value if min_value is not None else 'none'}|{max_value if max_value is not None else 'none'}",
            "kind": kind,
            "min_value": min_value,
            "max_value": max_value,
            "unit": unit,
            "min_inclusive": min_inclusive,
            "max_inclusive": max_inclusive,
            "raw_text": match.group(0).strip(),
            "substance_group_raw": re.sub(r"^[a-zđ]\)\s*", "", text[:match.start()].strip()).removeprefix("Với").strip(),
            "doc_id": doc_id,
        })
    return thresholds

def _parse_points(clause_id: str, clause_text: str, doc_id: str) -> list[dict[str, Any]]:
    starts = list(POINT_START.finditer(clause_text))
    points = []
    for index, start in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else len(clause_text)
        letter = start.group(1)
        text = clause_text[start.start():end].strip()
        point_id = f"{clause_id} điểm {letter}"
        points.append({
            "id": point_id,
            "letter": letter,
            "name": f"Điểm {letter} {clause_id}",
            "text": text,
            "doc_id": doc_id,
            "substances": find_substances(text),
            "thresholds": _parse_thresholds(point_id, text, doc_id),
        })
    return points

def parse_law_article(doc: Document) -> dict[str, Any]:
    """Deterministic (regex) extraction for one 'Điều' — law text is regular enough to skip the LLM."""
    article_id = doc.metadata["article"]                       # "Điều 251 BLHS"
    title = doc.metadata["title"].split(". ", 1)[-1]           # "Tội mua bán trái phép chất ma túy"
    body = FOOTNOTE.sub("", doc.content)
    starts = list(CLAUSE_START.finditer(body))
    clauses = []
    for index, start in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else len(body)
        text = body[start.start():end].strip()
        clause_id = f"{article_id} khoản {start.group(1)}"
        penalty = _parse_penalty(text)
        clauses.append({
            "id": clause_id,
            "number": int(start.group(1)),
            "penalty": penalty["penalty_raw"],
            **penalty,
            "text": text,
            "substances": find_substances(text),
            "points": _parse_points(clause_id, text, doc.id),
            "doc_id": doc.id,
        })
    return {
        "id": article_id,
        "law": doc.metadata.get("law", ""),
        "title": title,
        "doc_id": doc.id,
        "crime": normalize_crime(title) if title.startswith("Tội ") else None,
        "clauses": clauses,
    }

NEWS_EXTRACTION_PROMPT = """Bạn trích xuất knowledge graph từ một bài báo tiếng Việt về ma túy.
Chỉ dùng thông tin có trong bài. Trả về JSON đúng dạng:
{{"cases": [{{
  "name": "tên ngắn của vụ việc, ví dụ: Vụ mua bán 36kg ma túy tại TP.HCM",
  "summary": "1-2 câu tóm tắt",
  "date": "ngày xảy ra/xét xử nếu có, dạng YYYY-MM-DD hoặc chuỗi rỗng",
  "location": "tỉnh/thành phố, chuỗi rỗng nếu không rõ",
  "evidence_quote": "câu nguyên văn ngắn trong bài nói rõ nhất về vụ việc này, chuỗi rỗng nếu không có",
  "charges": ["tội danh, BẮT BUỘC chọn đúng nguyên văn từ DANH SÁCH TỘI DANH"],
  "substances": [{{"name": "tên chất, dùng tên chuẩn trong DANH SÁCH CHẤT nếu khớp", "amount": "khối lượng nguyên văn nếu có, ví dụ: hơn 9,6kg"}}],
  "people": [{{"name": "họ tên", "aliases": ["biệt danh"], "role": "bị cáo|bị can|nghi phạm|người liên quan|cán bộ",
               "charge": "tội danh của người này (từ DANH SÁCH TỘI DANH) hoặc chuỗi rỗng",
               "sentence": "mức án nguyên văn nếu có, ví dụ: tử hình, 8 năm tù"}}]
}}]}}
Bài không nói về vụ việc cụ thể (tuyên truyền, hội nghị...) thì trả về {{"cases": []}}.

DANH SÁCH TỘI DANH: {crimes}
DANH SÁCH CHẤT: {substances}

Tiêu đề: {title}
Nội dung:
{content}"""

NEWS_SUBSTANCE_ALIASES = {"ma túy đá": "Methamphetamine", "thuốc lắc": "MDMA"}
AMOUNT = re.compile(
    r"(?P<qualifier>hơn|gần|khoảng|trên|dưới)?\s*(?P<value>[\d.,]+)\s*(?P<unit>kg|kilôgam|gam|g|mililít|ml)\b",
    re.IGNORECASE,
)
SENTENCE_YEARS = re.compile(r"(\d+)\s*năm")
SENTENCE_MONTHS = re.compile(r"(\d+)\s*tháng")

def normalize_identity(value: str) -> str:
    """NFC + whitespace collapse for identity-bearing strings; never applied to raw evidence text."""
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", value)).strip()

def slugify(value: str) -> str:
    value = normalize_identity(value).lower()
    return re.sub(r"[^\w]+", "-", value, flags=re.UNICODE).strip("-")

def parse_sentence_months(sentence: str) -> int | None:
    text = sentence or ""
    years = SENTENCE_YEARS.search(text)
    months = SENTENCE_MONTHS.search(text)
    if not years and not months:
        return None
    return (int(years.group(1)) if years else 0) * 12 + (int(months.group(1)) if months else 0)

def parse_amount(text: str) -> dict[str, Any]:
    amount_text = (text or "").strip()
    match = AMOUNT.search(amount_text)
    if not match:
        return {"amount_text": amount_text, "amount_grams": None, "unit": None, "amount_qualifier": ""}
    value = _to_number(match.group("value"))
    unit_raw = match.group("unit").lower()
    if unit_raw in ("kg", "kilôgam"):
        amount_grams, unit = _clean_number(value * 1000), "g"
    elif unit_raw in ("gam", "g"):
        amount_grams, unit = value, "g"
    else:
        amount_grams, unit = None, "ml"
    return {
        "amount_text": amount_text,
        "amount_grams": amount_grams,
        "unit": unit,
        "amount_qualifier": (match.group("qualifier") or "").lower(),
    }

def canonical_substance(name: str) -> str | None:
    cleaned = normalize_identity(name)
    if not cleaned:
        return None
    alias = NEWS_SUBSTANCE_ALIASES.get(cleaned.lower())
    if alias:
        return alias
    return link_entity(cleaned, SUBSTANCES, normalize=lambda value: value.strip().lower())

def build_document_record(doc: Document) -> dict[str, Any]:
    """Provenance metadata for one source document; produces data only, no Neo4j writes."""
    metadata = doc.metadata
    title = normalize_identity(str(metadata.get("title", "")))
    return {
        "doc_id": doc.id,
        "title": title,
        "name": title or doc.id,
        "published_at": metadata.get("document_version") or metadata.get("retrieved_at", ""),
        "source_url": metadata.get("source_url", ""),
        "kb": metadata.get("kb", "news"),
    }

def make_case_key(primary_person: str, primary_crime: str, doc_id: str) -> str:
    person, crime = slugify(primary_person), slugify(primary_crime)
    if person and crime:
        return f"{person}__{crime}"
    return f"{person or 'unnamed'}__{crime or 'unknown'}__{doc_id}"

def _primary_person(people: list[dict]) -> dict | None:
    for role in ("bị cáo", "bị can", "nghi phạm"):
        for person in people:
            if role in person["role"].lower():
                return person
    return people[0] if people else None

def find_evidence_quote(document_text: str, needles: list[str]) -> str:
    """First sentence containing one of the needles; empty when no needle appears in the source text."""
    text = re.sub(r"(?m)^#.*$", "", document_text)
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        sentence = sentence.strip()
        if any(needle in sentence for needle in needles if needle):
            return sentence
    return ""

def classify_document_role(document_text: str, title: str, needles: list[str]) -> str:
    """Conservative: 'teaser' only when every needle first occurs in the final paragraph and not in the title."""
    offsets = [(document_text.find(needle), needle) for needle in needles if needle and document_text.find(needle) >= 0]
    if not offsets:
        return "primary"
    offset, needle = min(offsets)
    trailing_start = document_text.rfind("\n\n")
    if 0 <= trailing_start <= offset and needle.lower() not in title.lower():
        return "teaser"
    return "primary"

def _structure_case(case: dict, doc: Document, known_crimes: list[str], document: dict) -> dict[str, Any]:
    name = normalize_identity(case.get("name") or doc.metadata.get("title", doc.id))
    people = []
    for person in case.get("people", []) or []:
        person_name = normalize_identity(person.get("name") or "")
        if not person_name:
            continue
        aliases = [normalize_identity(alias) for alias in (person.get("aliases") or [])]
        charge_raw = normalize_identity(person.get("charge") or "")
        sentence = str(person.get("sentence") or "").strip()
        people.append({
            "name": person_name,
            "aliases": [alias for alias in aliases if alias],
            "role": normalize_identity(person.get("role") or ""),
            "charge": link_entity(charge_raw, known_crimes) or "",
            "charge_raw": charge_raw,
            "sentence": sentence,
            "sentence_text": sentence,
            "sentence_months": parse_sentence_months(sentence),
        })
    charges_raw = [normalize_identity(charge) for charge in (case.get("charges") or [])]
    charges_raw = [charge for charge in charges_raw if charge]
    charges = sorted({linked for linked in (link_entity(charge, known_crimes) for charge in charges_raw) if linked})
    substances = []
    for substance in case.get("substances", []) or []:
        name_raw = normalize_identity(substance.get("name") or "")
        if not name_raw:
            continue
        amount = parse_amount(substance.get("amount") or "")
        canonical = canonical_substance(name_raw)
        substances.append({
            "name": canonical or name_raw,
            "name_raw": name_raw,
            "canonical": canonical,
            "amount": amount["amount_text"],
            "amount_text": amount["amount_text"],
            "amount_grams": amount["amount_grams"],
            "unit": amount["unit"],
            "amount_qualifier": amount["amount_qualifier"],
        })
    primary_person = _primary_person(people)
    person_needle = primary_person["name"] if primary_person else ""
    primary_crime = charges[0] if charges else (charges_raw[0] if charges_raw else "")
    role_needles = [person_needle] if person_needle else ([primary_crime] if primary_crime else [])
    evidence = str(case.get("evidence_quote") or "").strip() or find_evidence_quote(doc.content, [person_needle, primary_crime])
    return {
        "name": name,
        "case_key": make_case_key(person_needle, primary_crime, doc.id),
        "summary": case.get("summary", ""),
        "date": case.get("date", ""),
        "location": case.get("location", ""),
        "charges": charges,
        "charges_raw": charges_raw,
        "people": people,
        "substances": substances,
        "provenance": {
            "doc_id": doc.id,
            "evidence_quote": evidence,
            "published_at": document["published_at"],
            "role_of_doc": classify_document_role(doc.content, document["title"], role_needles),
        },
    }

def extract_news_cases(doc: Document, llm_fn: Callable[[str], str], known_crimes: list[str]) -> list[dict]:
    """LLM extraction for one news article; charges and provenance are re-linked/structured in code."""
    document = build_document_record(doc)
    prompt = NEWS_EXTRACTION_PROMPT.format(
        crimes="; ".join(known_crimes), substances=", ".join(SUBSTANCES),
        title=doc.metadata.get("title", ""), content=doc.content[:12000],
    )
    try:
        cases = json.loads(llm_fn(prompt)).get("cases", [])
    except (json.JSONDecodeError, AttributeError):
        return []
    return [_structure_case(case, doc, known_crimes, document) for case in cases if isinstance(case, dict)]

def extract_news_document(doc: Document, llm_fn: Callable[[str], str], known_crimes: list[str]) -> dict[str, Any]:
    """Structured (document metadata + cases) for one news article; no Neo4j nodes/edges are created."""
    return {"document": build_document_record(doc), "cases": extract_news_cases(doc, llm_fn, known_crimes)}

# ----------------------------------------------------------------------------------------------
# Neo4j
# ----------------------------------------------------------------------------------------------

class Neo4jGraph:
    """Thin wrapper over the official neo4j driver."""

    def __init__(self, uri: str, user: str, password: str) -> None:
        from neo4j import GraphDatabase

        self.driver = GraphDatabase.driver(uri, auth=(user, password), notifications_min_severity="OFF")
        self.driver.verify_connectivity()

    def close(self) -> None:
        self.driver.close()

    def run(self, cypher: str, **params: Any) -> list[dict]:
        records, _, _ = self.driver.execute_query(cypher, params)
        return [record.data() for record in records]

    def reset(self) -> None:
        """Delete every node, relationship and constraint (bench_kg.py calls this before build_graph)."""
        self.run("MATCH (n) DETACH DELETE n")
        for row in self.run("SHOW CONSTRAINTS YIELD name RETURN name"):
            self.run(f"DROP CONSTRAINT `{row['name']}` IF EXISTS")

    def stats(self) -> dict[str, int]:
        nodes = self.run("MATCH (n) RETURN count(n) AS n")[0]["n"]
        rels = self.run("MATCH ()-[r]->() RETURN count(r) AS n")[0]["n"]
        return {"nodes": nodes, "relationships": rels}

    def seed_facts(self, question: str, doc_ids: list[str], skip_labels: tuple[str, ...] = (),
                   limit: int = 60) -> tuple[list[str], list[str]]:
        """Ontology-independent first step: seed nodes + their 1-hop edges as text facts.

        Seeds = nodes whose `doc_id` is in doc_ids, or whose `name`/`aliases` appear in the question.
        Returns (seed elementIds, facts). Nodes with a label in skip_labels are left out of the facts.
        """
        seeds = self.run(
            """
            MATCH (n)
            WHERE n.doc_id IN $doc_ids
               OR (n.name IS :: STRING AND size(n.name) >= 3 AND toLower($q) CONTAINS toLower(n.name))
               OR any(a IN coalesce(n.aliases, []) WHERE size(a) >= 3 AND toLower($q) CONTAINS toLower(a))
            RETURN elementId(n) AS id
            """,
            q=question, doc_ids=doc_ids,
        )
        seed_ids = [row["id"] for row in seeds]
        edges = self.run(
            """
            MATCH (s)-[r]-(m)
            WHERE elementId(s) IN $ids
              AND none(l IN labels(s) + labels(m) WHERE l IN $skip)
            WITH DISTINCT r LIMIT $limit
            WITH startNode(r) AS a, r, endNode(r) AS b
            RETURN labels(a)[0] AS a_label, coalesce(a.name, a.id) AS a_name, type(r) AS rel,
                   properties(r) AS props, labels(b)[0] AS b_label, coalesce(b.name, b.id) AS b_name
            """,
            ids=seed_ids, skip=list(skip_labels), limit=limit,
        )
        facts = []
        for e in edges:
            props = ", ".join(f"{k}: {v}" for k, v in e["props"].items() if v)
            facts.append(f"({e['a_label']}: {e['a_name']}) -[{e['rel']}{' {' + props + '}' if props else ''}]-> "
                         f"({e['b_label']}: {e['b_name']})")
        return seed_ids, facts

    # ---------------------------------------------------------------- KG-2 — domain writers

    def create_constraints(self) -> None:
        for label, key in [("Article", "id"), ("Clause", "id"), ("Point", "id"), ("QuantityThreshold", "id"),
                           ("Crime", "name"), ("Substance", "name"), ("Case", "case_key"),
                           ("Person", "name"), ("Document", "doc_id")]:
            self.run(f"CREATE CONSTRAINT IF NOT EXISTS FOR (n:{label}) REQUIRE n.{key} IS UNIQUE")

    def add_law_article(self, article: dict) -> None:
        self.run(
            """
            MERGE (a:Article {id: $id})
              SET a.title = $title, a.law = $law, a.doc_id = $doc_id
            FOREACH (crime IN CASE WHEN $crime IS NULL THEN [] ELSE [$crime] END |
                MERGE (c:Crime {name: crime})
                MERGE (a)-[:DEFINES]->(c))
            """,
            id=article["id"], title=article["title"], law=article["law"],
            doc_id=article["doc_id"], crime=article["crime"],
        )
        for clause in article["clauses"]:
            self.add_clause(article, clause)

    def add_clause(self, article: dict, clause: dict) -> None:
        self.run(
            """
            MATCH (a:Article {id: $article_id})
            MERGE (cl:Clause {id: $id})
              SET cl.number = $number, cl.text = $text,
                  cl.penalty_raw = $penalty_raw, cl.penalty_min_months = $penalty_min_months,
                  cl.penalty_max_months = $penalty_max_months, cl.penalty_life = $penalty_life,
                  cl.penalty_death = $penalty_death, cl.penalty_kind = $penalty_kind,
                  cl.doc_id = $doc_id
            MERGE (a)-[:HAS_CLAUSE]->(cl)
            FOREACH (substance IN $substances |
                MERGE (s:Substance {name: substance})
                MERGE (cl)-[:MENTIONS]->(s))
            """,
            article_id=article["id"], id=clause["id"], number=clause["number"], text=clause["text"],
            penalty_raw=clause["penalty_raw"], penalty_min_months=clause["penalty_min_months"],
            penalty_max_months=clause["penalty_max_months"], penalty_life=clause["penalty_life"],
            penalty_death=clause["penalty_death"], penalty_kind=clause["penalty_kind"],
            doc_id=clause["doc_id"], substances=clause["substances"],
        )
        for point in clause["points"]:
            self.add_point(clause, point)

    def add_point(self, clause: dict, point: dict) -> None:
        self.run(
            """
            MATCH (cl:Clause {id: $clause_id})
            MERGE (p:Point {id: $id})
              SET p.letter = $letter, p.name = $name, p.text = $text, p.doc_id = $doc_id
            MERGE (cl)-[:HAS_POINT]->(p)
            FOREACH (substance IN $substances |
                MERGE (s:Substance {name: substance})
                MERGE (p)-[:MENTIONS]->(s))
            """,
            clause_id=clause["id"], id=point["id"], letter=point["letter"], name=point["name"],
            text=point["text"], doc_id=point["doc_id"], substances=point["substances"],
        )
        for threshold in point["thresholds"]:
            self.add_threshold(point, threshold)

    def add_threshold(self, point: dict, threshold: dict) -> None:
        applies_to = [] if threshold["kind"] == "equivalence" else point["substances"]
        self.run(
            """
            MATCH (p:Point {id: $point_id})
            MERGE (t:QuantityThreshold {id: $id})
              SET t.kind = $kind, t.min_value = $min_value, t.max_value = $max_value, t.unit = $unit,
                  t.min_inclusive = $min_inclusive, t.max_inclusive = $max_inclusive,
                  t.raw_text = $raw_text, t.substance_group_raw = $substance_group_raw, t.doc_id = $doc_id
            MERGE (p)-[:HAS_THRESHOLD]->(t)
            FOREACH (substance IN $applies_to |
                MERGE (s:Substance {name: substance})
                MERGE (t)-[:APPLIES_TO]->(s))
            """,
            point_id=point["id"], id=threshold["id"], kind=threshold["kind"],
            min_value=threshold["min_value"], max_value=threshold["max_value"], unit=threshold["unit"],
            min_inclusive=threshold["min_inclusive"], max_inclusive=threshold["max_inclusive"],
            raw_text=threshold["raw_text"], substance_group_raw=threshold["substance_group_raw"],
            doc_id=threshold["doc_id"], applies_to=applies_to,
        )

    def add_news_document(self, document: dict) -> None:
        self.run(
            """
            MERGE (d:Document {doc_id: $doc_id})
              SET d.name = $name, d.title = $title, d.published_at = $published_at,
                  d.source_url = $source_url, d.kb = $kb
            """,
            **document,
        )

    def add_case(self, case: dict) -> None:
        provenance = case["provenance"]
        self.run(
            """
            MATCH (d:Document {doc_id: $doc_id})
            MERGE (k:Case {case_key: $case_key})
              ON CREATE SET k.name = $name, k.summary = $summary, k.date = $date, k.location = $location
              ON MATCH SET k.name = coalesce(k.name, $name)
            MERGE (k)-[r:DOCUMENTED_BY]->(d)
              SET r.evidence_quote = $evidence_quote, r.published_at = $published_at, r.role_of_doc = $role_of_doc
            """,
            doc_id=provenance["doc_id"], case_key=case["case_key"], name=case["name"],
            summary=case["summary"], date=case["date"], location=case["location"],
            evidence_quote=provenance["evidence_quote"], published_at=provenance["published_at"],
            role_of_doc=provenance["role_of_doc"],
        )
        for crime in case["charges"]:
            self.run(
                """
                MATCH (k:Case {case_key: $case_key})
                MERGE (c:Crime {name: $crime})
                MERGE (k)-[:CHARGED_WITH]->(c)
                """,
                case_key=case["case_key"], crime=crime,
            )
        for person in case["people"]:
            self.run(
                """
                MATCH (k:Case {case_key: $case_key})
                MERGE (p:Person {name: $name})
                  SET p.aliases = coalesce(p.aliases, []) + [a IN $aliases WHERE NOT a IN coalesce(p.aliases, [])]
                MERGE (p)-[r:INVOLVED_IN]->(k)
                  SET r.role = $role, r.charge = $charge,
                      r.sentence_text = $sentence_text, r.sentence_months = $sentence_months
                """,
                case_key=case["case_key"], name=person["name"], aliases=person["aliases"],
                role=person["role"], charge=person["charge"],
                sentence_text=person["sentence_text"], sentence_months=person["sentence_months"],
            )
        for substance in case["substances"]:
            self.run(
                """
                MATCH (k:Case {case_key: $case_key})
                MERGE (s:Substance {name: $name})
                MERGE (k)-[r:INVOLVES]->(s)
                  SET r.amount_text = $amount_text, r.amount_grams = $amount_grams,
                      r.unit = $unit, r.amount_qualifier = $amount_qualifier
                """,
                case_key=case["case_key"], name=substance["name"],
                amount_text=substance["amount_text"], amount_grams=substance["amount_grams"],
                unit=substance["unit"], amount_qualifier=substance["amount_qualifier"],
            )

    # ---------------------------------------------------------------- KG-3

    def context(self, question: str, doc_ids: list[str], max_facts: int = 60) -> list[str]:
        """Graph facts for a question: provenance seeds, case expansion, then the legal basis and thresholds."""
        facts: list[str] = []
        seen: set[str] = set()

        def add(fact: str) -> None:
            fact = re.sub(r"\s+", " ", fact).strip()
            if fact and fact not in seen:
                seen.add(fact)
                facts.append(fact)

        question_substances = find_substances(question)
        question_numbers = set(re.findall(r"[Đđ]iều\s+(\d+)", question))
        question_tokens = {token for token in re.findall(r"\w+", question.lower()) if len(token) >= 3}
        case_keys: list[str] = []

        for row in self.run(
            """
            MATCH (d:Document)<-[r:DOCUMENTED_BY]-(k:Case)
            WHERE d.doc_id IN $doc_ids
            RETURN k.case_key AS case_key, k.name AS case_name, d.doc_id AS doc_id, d.name AS doc_name,
                   r.role_of_doc AS role, r.evidence_quote AS quote
            ORDER BY d.doc_id, k.case_key
            """,
            doc_ids=doc_ids,
        ):
            add(f"Vụ việc: {row['case_name']} (case_key={row['case_key']}) | nguồn={row['doc_id']} (\"{row['doc_name']}\") | vai trò nguồn={row['role'] or 'primary'}")
            if row["quote"]:
                add(f"Bằng chứng ({row['doc_id']}): {row['quote'][:200]}")
            if row["case_key"] not in case_keys:
                case_keys.append(row["case_key"])

        for row in self.run(
            """
            MATCH (p:Person)-[:INVOLVED_IN]->(k:Case)
            WHERE toLower($q) CONTAINS toLower(p.name)
               OR any(a IN coalesce(p.aliases, []) WHERE size(a) >= 4 AND toLower($q) CONTAINS toLower(a))
            RETURN DISTINCT k.case_key AS case_key, k.name AS case_name
            ORDER BY case_key
            """,
            q=question,
        ):
            if row["case_key"] not in case_keys:
                case_keys.append(row["case_key"])
            add(f"Vụ việc khớp tên người trong câu hỏi: {row['case_name']} (case_key={row['case_key']})")

        aggregate_keys: list[str] = []
        for row in self.run(
            """
            MATCH (k:Case)-[:INVOLVES]->(s:Substance)
            WHERE s.name IN $names
            RETURN DISTINCT k.case_key AS case_key, k.name AS case_name
            ORDER BY case_key
            """,
            names=question_substances,
        ):
            if row["case_key"] not in case_keys and row["case_key"] not in aggregate_keys:
                aggregate_keys.append(row["case_key"])

        case_names: dict[str, str] = {}
        if case_keys or aggregate_keys:
            for row in self.run(
                "MATCH (k:Case) WHERE k.case_key IN $keys RETURN k.case_key AS case_key, k.name AS case_name",
                keys=case_keys + aggregate_keys,
            ):
                case_names[row["case_key"]] = row["case_name"]
        if aggregate_keys:
            for row in self.run(
                """
                MATCH (k:Case) WHERE k.case_key IN $keys
                OPTIONAL MATCH (k)-[:DOCUMENTED_BY]->(d:Document)
                OPTIONAL MATCH (p:Person)-[:INVOLVED_IN]->(k)
                OPTIONAL MATCH (k)-[iv:INVOLVES]->(s:Substance)
                RETURN k.case_key AS case_key, collect(DISTINCT p.name) AS persons,
                       collect(DISTINCT s.name + ' ' + coalesce(iv.amount_text, '')) AS substances,
                       collect(DISTINCT d.doc_id + ' "' + d.name + '"') AS sources
                ORDER BY case_key
                """,
                keys=aggregate_keys,
            ):
                persons = ", ".join(name for name in row["persons"] if name)
                substances = "; ".join(item.strip() for item in row["substances"] if item.strip())
                sources = "; ".join(item for item in row["sources"] if item)
                add(f"Vụ việc liên quan {', '.join(question_substances)}: {case_names.get(row['case_key'], row['case_key'])} | nhân vật: {persons or 'không rõ'} | chất: {substances or 'không rõ'} | nguồn: {sources or 'không rõ'}")
        if case_keys:
            for row in self.run(
                """
                MATCH (p:Person)-[r:INVOLVED_IN]->(k:Case)
                WHERE k.case_key IN $keys
                RETURN k.case_key AS case_key, p.name AS name, p.aliases AS aliases, r.role AS role,
                       r.charge AS charge, r.sentence_text AS sentence, r.sentence_months AS months
                ORDER BY k.case_key, name
                """,
                keys=case_keys,
            ):
                details = ", ".join(part for part in [
                    f"vai trò={row['role']}" if row["role"] else "",
                    f"tội={row['charge']}" if row["charge"] else "",
                    f"mức án={row['sentence']}" if row["sentence"] else "",
                    f"({row['months']} tháng)" if row["months"] is not None and not row["sentence"] else "",
                ] if part)
                aliases = f" [bí danh: {', '.join(row['aliases'])}]" if row["aliases"] else ""
                add(f"{row['name']}{aliases} --INVOLVED_IN--> {case_names.get(row['case_key'], row['case_key'])} | {details}")
            for row in self.run(
                """
                MATCH (k:Case)-[:CHARGED_WITH]->(c:Crime)
                WHERE k.case_key IN $keys
                RETURN k.case_key AS case_key, c.name AS crime
                ORDER BY case_key, crime
                """,
                keys=case_keys,
            ):
                add(f"{case_names.get(row['case_key'], row['case_key'])} --CHARGED_WITH--> {row['crime']}")
            for row in self.run(
                """
                MATCH (k:Case)-[r:INVOLVES]->(s:Substance)
                WHERE k.case_key IN $keys
                RETURN k.case_key AS case_key, s.name AS substance, r.amount_text AS amount_text,
                       r.amount_grams AS amount_grams, r.unit AS unit, r.amount_qualifier AS amount_qualifier
                ORDER BY case_key, substance
                """,
                keys=case_keys,
            ):
                grams = f" = {row['amount_grams']} {row['unit']}" if row["amount_grams"] is not None and row["unit"] else ""
                qualifier = f" [{row['amount_qualifier']}]" if row["amount_qualifier"] else ""
                add(f"{case_names.get(row['case_key'], row['case_key'])} --INVOLVES--> {row['substance']} | {row['amount_text'] or 'không rõ khối lượng'}{grams}{qualifier}")

        article_ids: set[str] = set()
        law_seed_articles: set[str] = set()
        if case_keys:
            for row in self.run(
                """
                MATCH (k:Case)-[:CHARGED_WITH]->(c:Crime)<-[:DEFINES]-(a:Article)
                WHERE k.case_key IN $keys
                RETURN DISTINCT a.id AS id, c.name AS crime
                ORDER BY id, crime
                """,
                keys=case_keys,
            ):
                article_ids.add(row["id"])
                add(f"{row['id']} --DEFINES--> {row['crime']}")
        for row in self.run("MATCH (a:Article) WHERE a.doc_id IN $doc_ids RETURN a.id AS id", doc_ids=doc_ids):
            article_ids.add(row["id"])
            law_seed_articles.add(row["id"])
        if question_numbers:
            for row in self.run("MATCH (a:Article) RETURN a.id AS id"):
                match = re.match(r"Điều (\d+)", row["id"])
                if match and match.group(1) in question_numbers:
                    article_ids.add(row["id"])
                    law_seed_articles.add(row["id"])
        for row in self.run(
            "MATCH (a:Article) WHERE a.id IN $ids RETURN a.id AS id, a.title AS title, a.doc_id AS doc_id",
            ids=sorted(article_ids),
        ):
            add(f"Điều luật: {row['id']} | {row['title']} | doc_id={row['doc_id']}")

        case_substances: set[str] = set()
        if case_keys:
            for row in self.run(
                "MATCH (k:Case)-[:INVOLVES]->(s:Substance) WHERE k.case_key IN $keys RETURN DISTINCT s.name AS name",
                keys=case_keys,
            ):
                case_substances.add(row["name"])
        relevant_substances = set(question_substances) | case_substances

        clauses_by_article: dict[str, list[dict]] = {}
        for row in self.run(
            """
            MATCH (a:Article)-[:HAS_CLAUSE]->(cl:Clause)
            WHERE a.id IN $ids
            OPTIONAL MATCH (cl)-[:MENTIONS]->(s:Substance)
            WITH a, cl, collect(DISTINCT s.name) AS mentions
            RETURN a.id AS article_id, cl.number AS number, cl.text AS text, cl.penalty_raw AS penalty,
                   cl.penalty_min_months AS min_months, cl.penalty_max_months AS max_months,
                   cl.penalty_life AS life, cl.penalty_death AS death, mentions
            ORDER BY a.id, cl.number
            """,
            ids=sorted(article_ids),
        ):
            clauses_by_article.setdefault(row["article_id"], []).append(row)

        for article_id, clauses in clauses_by_article.items():
            selected = {clause["number"] for clause in clauses if clause["number"] == 1}
            for clause in clauses:
                if relevant_substances and set(clause["mentions"]) & relevant_substances:
                    selected.add(clause["number"])
            ranked = [clause for clause in clauses if clause["life"] or clause["death"] or clause["min_months"] or clause["max_months"]]
            if ranked:
                highest = max(ranked, key=lambda clause: (bool(clause["death"]), bool(clause["life"]),
                                                          clause["max_months"] or 0, clause["min_months"] or 0))
                selected.add(highest["number"])
            if article_id in law_seed_articles and question_tokens:
                scored = sorted(clauses, key=lambda clause: (-sum(token in clause["text"].lower() for token in question_tokens), clause["number"]))
                for clause in scored[:3]:
                    if sum(token in clause["text"].lower() for token in question_tokens) > 0:
                        selected.add(clause["number"])
            for clause in sorted((clause for clause in clauses if clause["number"] in selected), key=lambda clause: clause["number"]):
                text = re.sub(r"\s+", " ", clause["text"])[:400]
                add(f"[{article_id}] khoản {clause['number']} | hình phạt: {clause['penalty'] or 'không quy định'} | nội dung: {text}")

        case_amounts: dict[str, dict[str, list[tuple[Any, str]]]] = {}
        for row in self.run(
            """
            MATCH (k:Case)-[r:INVOLVES]->(s:Substance)
            WHERE k.case_key IN $keys
            RETURN k.case_key AS case_key, s.name AS substance, r.amount_grams AS grams, r.unit AS unit
            """,
            keys=case_keys,
        ):
            if row["grams"] is not None and row["unit"]:
                case_amounts.setdefault(row["case_key"], {}).setdefault(row["substance"], []).append((row["grams"], row["unit"]))
        case_articles: dict[str, set[str]] = {}
        for row in self.run(
            """
            MATCH (k:Case)-[:CHARGED_WITH]->(:Crime)<-[:DEFINES]-(a:Article)
            WHERE k.case_key IN $keys
            RETURN k.case_key AS case_key, a.id AS id
            """,
            keys=case_keys,
        ):
            case_articles.setdefault(row["case_key"], set()).add(row["id"])
        matched: set[tuple[str, str, int, str, str]] = set()
        for case_key, substance_amounts in case_amounts.items():
            session_articles = sorted(case_articles.get(case_key, set()))
            if not session_articles:
                continue
            for row in self.run(
                """
                MATCH (t:QuantityThreshold)-[:APPLIES_TO]->(s:Substance)
                MATCH (pt:Point)-[:HAS_THRESHOLD]->(t)
                MATCH (cl:Clause)-[:HAS_POINT]->(pt)
                MATCH (a:Article)-[:HAS_CLAUSE]->(cl)
                WHERE a.id IN $articles AND s.name IN $substances
                RETURN a.id AS article_id, cl.number AS clause, pt.letter AS letter, pt.text AS point_text,
                       t.kind AS kind, t.min_value AS min_value, t.max_value AS max_value, t.unit AS unit,
                       t.max_inclusive AS max_inclusive, t.raw_text AS raw_text, s.name AS substance
                ORDER BY a.id, cl.number, pt.letter, s.name
                """,
                articles=session_articles, substances=sorted(substance_amounts),
            ):
                for amount, unit in substance_amounts.get(row["substance"], []):
                    if unit != row["unit"]:
                        continue
                    if row["kind"] == "quantity_min":
                        is_match = row["min_value"] is not None and amount >= row["min_value"]
                    elif row["kind"] == "quantity_range":
                        is_match = (row["min_value"] is not None and amount >= row["min_value"]
                                    and (amount <= row["max_value"] if row["max_inclusive"] else amount < row["max_value"]))
                    else:
                        is_match = False
                    key = (case_key, row["article_id"], row["clause"], row["letter"], row["substance"])
                    if not is_match or key in matched:
                        continue
                    matched.add(key)
                    add(f"{case_names.get(case_key, case_key)}: {row['article_id']} khoản {row['clause']} điểm {row['letter']} áp dụng cho {row['substance']} {amount} {unit} ({row['raw_text']})")
                    point_text = re.sub(r"\s+", " ", row["point_text"])[:300]
                    add(f"[{row['article_id']}] khoản {row['clause']} điểm {row['letter']}: {point_text}")

        return facts[:max_facts]

# ---------------------------------------------------------------------------------------------- KG-2

def _json_llm(llm_fn: Callable[..., str]) -> Callable[[str], str]:
    """Adapt llm_fn so news extraction requests JSON mode when the callable supports it."""
    try:
        supports_json_mode = "json_mode" in inspect.signature(llm_fn).parameters
    except (TypeError, ValueError):
        supports_json_mode = False
    if supports_json_mode:
        return lambda prompt: llm_fn(prompt, json_mode=True)
    return llm_fn

def build_graph(graph: Neo4jGraph, law_docs: list[Document], news_docs: list[Document],
                llm_fn: Callable[..., str]) -> None:
    """Load both KBs into an empty graph. llm_fn(prompt, json_mode=False) -> str (metered OpenAI chat)."""
    graph.create_constraints()
    articles = [parse_law_article(doc) for doc in law_docs]
    crimes = sorted({article["crime"] for article in articles if article["crime"]})
    for article in articles:
        graph.add_law_article(article)
    json_llm = _json_llm(llm_fn)
    for doc in news_docs:
        extracted = extract_news_document(doc, json_llm, crimes)
        graph.add_news_document(extracted["document"])
        for case in extracted["cases"]:
            graph.add_case(case)

# ---------------------------------------------------------------------------------------------- KG-4

GRAPH_PROMPT = """Bạn trả lời câu hỏi tiếng Việt dựa CHỈ trên bằng chứng được cung cấp.

QUY TẮC:
- Chỉ dùng dữ kiện knowledge graph và đoạn văn bản bên dưới; không bịa thêm.
- Ưu tiên dữ kiện đồ thị cho liên kết xuyên tài liệu (người ↔ vụ ↔ tội ↔ điều luật).
- Giữ nguyên số Điều/khoản/điểm khi có.
- Nếu bằng chứng không đủ, trả lời "không đủ thông tin".

CÂU HỎI:
{question}

DỮ KIỆN KNOWLEDGE GRAPH:
{facts}

ĐOẠN VĂN BẢN TRUY XUẤT:
{chunks}

TRẢ LỜI:"""

class GraphRAGAgent:
    """Hybrid GraphRAG: the same vector top-k as flat RAG, plus facts expanded from the graph."""

    def __init__(self, store: EmbeddingStore, graph: Neo4jGraph, llm_fn: Callable[[str], str]) -> None:
        self.store = store
        self.graph = graph
        self.llm_fn = llm_fn

    def answer(self, question: str, top_k: int = 3) -> str:
        chunks = self.store.search(question, top_k=top_k)
        doc_ids: list[str] = []
        for chunk in chunks:
            doc_id = (chunk.get("metadata") or {}).get("doc_id")
            if doc_id and doc_id not in doc_ids:
                doc_ids.append(doc_id)
        facts = self.graph.context(question, doc_ids)
        fact_text = "\n".join(f"- {fact}" for fact in facts) or "- (không có dữ kiện đồ thị)"
        chunk_text = "\n\n".join(f"[{index}] {chunk['content']}" for index, chunk in enumerate(chunks, start=1)) or "(không có đoạn văn bản)"
        prompt = GRAPH_PROMPT.format(facts=fact_text, chunks=chunk_text, question=question)
        return self.llm_fn(prompt)
