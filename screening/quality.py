"""Quality signals that need only the paper's own text and metadata: tortured phrases, scope match,
code/data availability statements and ORCID iDs. All local; every flag carries the text it found."""
import json
import re

from .db import add_signal, audit, start_run
from .duplicates import WORD, words

VERSION = "1"

# Paraphrasing tools turn standard terms into nonsense synonyms to hide copied text (Cabanac et al., 2021,
# "Tortured phrases"). A real author practically never writes these.
TORTURED = {
    "counterfeit consciousness": "artificial intelligence", "profound learning": "deep learning",
    "profound neural organization": "deep neural network", "neural organization": "neural network",
    "fake neural organization": "artificial neural network", "repetitive neural organization": "recurrent neural network",
    "irregular woodland": "random forest", "arbitrary woodland": "random forest", "haphazard woodland": "random forest",
    "colossal information": "big data", "huge information": "big data", "flag to commotion": "signal to noise",
    "bosom malignancy": "breast cancer", "bosom disease": "breast cancer", "lung malignant growth": "lung cancer",
    "mean square blunder": "mean square error", "bolster vector machine": "support vector machine",
    "k closest neighbor": "k-nearest neighbor", "straight relapse": "linear regression",
    "calculated relapse": "logistic regression", "head part examination": "principal component analysis",
    "web of things": "internet of things", "remote sensor organization": "wireless sensor network",
    "cloud figuring": "cloud computing", "edge figuring": "edge computing", "quantum figuring": "quantum computing",
    "pc vision": "computer vision", "picture handling": "image processing",
    "characteristic language preparing": "natural language processing", "opinion investigation": "sentiment analysis",
    "discourse acknowledgment": "speech recognition", "facial acknowledgment": "facial recognition",
    "shrewd lattice": "smart grid", "savvy city": "smart city", "sun powered cell": "solar cell",
    "irregular access memory": "random access memory", "worldwide situating framework": "global positioning system",
    "bit mistake rate": "bit error rate", "remote correspondence": "wireless communication",
    "gullible bayes": "naive Bayes", "angle plunge": "gradient descent", "slope plummet": "gradient descent",
    "enactment work": "activation function", "misfortune work": "loss function",
    "highlight extraction": "feature extraction", "exchange learning": "transfer learning",
    "fortification learning": "reinforcement learning", "administered learning": "supervised learning",
    "unaided learning": "unsupervised learning", "dark opening": "black hole",
}
STOP = {"and", "the", "for", "with", "from", "into", "based", "using", "its", "their", "of", "in", "on", "to"}
SHARE = re.compile(r"(?:code|data|datasets?|implementation|software|models?|scripts?)\b[^.]{0,80}?\b(?:is|are|will be|"
                   r"has been|have been)\s+(?:made\s+|publicly\s+|freely\s+|openly\s+)*(?:available|released|"
                   r"provided|accessible|shared)[^.]{0,120}", re.I)
SHARE_LINK = re.compile(r"\b(?:github\.com|gitlab\.com|bitbucket\.org|zenodo\.org|figshare\.com|osf\.io|"
                        r"anonymous\.4open\.science|huggingface\.co|kaggle\.com|codeocean\.com)/\S+", re.I)
ON_REQUEST = re.compile(r"(?:available|provided)\s+(?:from the (?:corresponding )?authors?\s+)?(?:up)?on\s+"
                        r"(?:reasonable\s+)?request", re.I)
ORCID = re.compile(r"(\d{4})-?(\d{4})-?(\d{4})-?(\d{3}[\dX])", re.I)


def _stem(w: str) -> str:
    return w[:-1] if len(w) > 4 and w.endswith("s") else w


def tortured(ws: list[str]) -> list[dict]:
    text = " " + " ".join(ws) + " "
    found = []
    for phrase, usual in TORTURED.items():
        key = " " + " ".join(WORD.findall(phrase)) + " "
        if n := text.count(key):
            i = text.index(key)
            found.append({"found": phrase, "usual_term": usual, "times": n,
                          "context": "…" + text[max(0, i - 60):i + len(key) + 60].strip() + "…"})
    # "neural organization" inside "fake neural organization" is the same finding
    return [f for f in found if not any(f["found"] != g["found"] and f["found"] in g["found"] for g in found)]


def scope(struct: dict, topics: list[str]) -> list[dict]:
    """Share of each topic's words found in the title, abstract and keywords, best first."""
    head = " ".join(struct.get(k) or "" for k in ("title", "abstract", "keywords"))
    have = {_stem(w) for w in WORD.findall(head.lower())}
    out = []
    for t in topics:
        terms = {_stem(w) for w in WORD.findall(t.lower()) if w not in STOP and len(w) > 2}
        if terms:
            hit = sorted(terms & have)
            out.append({"topic": t, "match": round(len(hit) / len(terms), 2), "words_found": hit})
    return sorted(out, key=lambda x: -x["match"])


def orcid_ok(value: str) -> bool:
    """ISO 7064 mod 11-2 check digit, as ORCID defines it."""
    m = ORCID.fullmatch(re.sub(r"^(?:https?://)?(?:www\.)?orcid\.org/", "", value.strip(), flags=re.I))
    if not m:
        return False
    digits = "".join(m.groups()).upper()
    total = 0
    for ch in digits[:-1]:
        total = (total + int(ch)) * 2
    check = (12 - total % 11) % 11
    return digits[-1] == ("X" if check == 10 else str(check))


def run_quality(con, paper_id: int, rules) -> None:
    row = con.execute("SELECT sections_json FROM paper WHERE id=?", (paper_id,)).fetchone()
    run = start_run(con, paper_id, "quality", VERSION, {k: rules[k] for k in ("scope", "quality")})
    struct = None
    try:
        struct = json.loads(row["sections_json"]) if row["sections_json"] else None
    except ValueError:
        pass
    if isinstance(struct, dict):
        ws = words(struct)
        if found := tortured(ws):
            add_signal(con, run, "tortured_phrases", "warn",
                       f"{len(found)} garbled stand-in(s) for standard terms, e.g. \"{found[0]['found']}\" for "
                       f"\"{found[0]['usual_term']}\": typical of text run through a paraphrasing tool.",
                       {"phrases": found})
        if topics := rules["scope"]["topics"]:
            fit = scope(struct, topics)
            if not fit or fit[0]["match"] == 0:
                add_signal(con, run, "outside_scope", "warn", "The title, abstract and keywords share no words with "
                           "any of the conference topics: check that the paper fits the call.", {"topics": fit})
            else:
                add_signal(con, run, "scope_match", "info", f"Closest topic: {fit[0]['topic']}.", {"topics": fit[:3]})
        body = " ".join([struct.get("abstract") or ""] + [s.get("text") or "" for s in struct.get("sections") or []
                                                           if isinstance(s, dict)])
        said = [m.group(0)[:200] for m in SHARE.finditer(body)][:3] + [m.group(0)[:120] for m in SHARE_LINK.finditer(body)][:3]
        if said:
            add_signal(con, run, "code_data_statement", "info", "The paper says where its code or data is.",
                       {"statements": said, "on_request_only": bool(ON_REQUEST.search(body)) and not SHARE_LINK.search(body)})
        elif rules["quality"]["require_code_statement"]:
            add_signal(con, run, "no_code_data_statement", "warn", "No statement on where the code or data can be found.")
    bad = [{"author": a["name"], "orcid": a["orcid"]} for a in
           con.execute("SELECT name, orcid FROM author WHERE paper_id=? AND orcid IS NOT NULL AND orcid != ''", (paper_id,))
           if not orcid_ok(a["orcid"])]
    if bad:
        add_signal(con, run, "orcid_invalid", "warn", f"{len(bad)} ORCID iD(s) in the author list are not valid "
                   "(wrong format or check digit).", {"authors": bad})
    audit(con, "system", "quality", "paper", paper_id, {"run_id": run})
    con.commit()


assert orcid_ok("0000-0002-1825-0097") and orcid_ok("https://orcid.org/0000-0001-5109-3700")
assert not orcid_ok("0000-0002-1825-0098") and orcid_ok("0000-0002-1694-233X")
assert [f["found"] for f in tortured("we use profound learning and irregular woodland".split())] == [
    "profound learning", "irregular woodland"]
